import os
import logging
from tempfile import mkstemp
from tarfile import TarFile
from collections import deque
from binascii import hexlify
from time import time
from threading import RLock

from Tribler.dispersy.taskmanager import TaskManager, LoopingCall
from Tribler.dispersy.candidate import Candidate

from .session import Session, DEFAULT_BLOCK_SIZE, DEFAULT_TIMEOUT
from .packet import (encode_packet, decode_packet, OPCODE_RRQ, OPCODE_WRQ, OPCODE_ACK, OPCODE_DATA, OPCODE_OACK,
                     OPCODE_ERROR, ERROR_DICT)
from .exception import InvalidPacketException

MAX_INT32 = 2 ** 16 - 1

DIR_SEPARATOR = u":"
DIR_PREFIX = u"dir" + DIR_SEPARATOR


class TftpHandler(TaskManager):
    """
    This is the TFTP handler that should be registered at the RawServer to handle TFTP packets.
    """

    def __init__(self, session, root_dir, endpoint, prefix, block_size=DEFAULT_BLOCK_SIZE, timeout=DEFAULT_TIMEOUT):
        """ The constructor.
        :param session:    The tribler session.
        :param root_dir:   The root directory to use.
        :param endpoint:   The endpoint to use.
        :param prefix:     The prefix to use.
        :param block_size: Transmission block size.
        :param timeout:    Transmission timeout.
        """
        super(TftpHandler, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)

        self.session = session
        self.root_dir = root_dir
        # check the root directory if it is valid
        if not os.path.exists(root_dir):
            try:
                os.makedirs(self.root_dir)
            except OSError as ex:
                self._logger.critical(u"Could not create root_dir %s: %s", root_dir, ex)
                raise ex
        if os.path.exists(root_dir) and not os.path.isdir(root_dir):
            msg = u"root_dir is not a directory: %s" % root_dir
            self._logger.critical(msg)
            raise Exception(msg)

        self.endpoint = endpoint
        self.prefix = prefix

        self.block_size = block_size
        self.timeout = timeout

        self._timeout_check_interval = 2

        self._session_lock = RLock()
        self._session_dict = {}

    def initialize(self):
        """ Initializes the TFTP service. We create a UDP socket and a server session.
        """
        self.endpoint.listen_to(self.prefix, self.data_came_in)
        # start a looping call that checks timeout
        self.register_task("tftp timeout check",
                           LoopingCall(self._check_timeout)).start(self._timeout_check_interval, now=True)

    def shutdown(self):
        """ Shuts down the TFTP service.
        """
        self.cancel_all_pending_tasks()

        self._session_dict = None

    def download_file(self, file_name, ip, port, success_callback=None, failure_callback=None):
        """ Downloads a file from a remote host.
        :param file_name: The file name of the file to be downloaded.
        :param ip:        The IP of the remote host.
        :param port:      The port of the remote host.
        :param success_callback: The success callback.
        :param failure_callback: The failure callback.
        """
        self._logger.debug(u"Start downloading %s from %s:%s", file_name, ip, port)
        session = Session(True, (ip, port), OPCODE_RRQ, file_name, '', None,
                          success_callback=success_callback, failure_callback=failure_callback)

        with self._session_lock:
            if (ip, port) not in self._session_dict:
                self._session_dict[(ip, port)] = deque()
            self._session_dict[(ip, port)].append(session)

            if session == self._session_dict[(ip, port)][0]:
                self._send_request_packet(session)
            else:
                session.next_func = lambda s = session: self._send_request_packet(s)

    def _check_timeout(self):
        """ A scheduled task that checks for timeout.
        """
        # TODO: make a nicer way to check if we are shutting down
        if self._session_dict is None:
            return

        with self._session_lock:
            for key, session_queue in self._session_dict.items():
                # only check the first session (the active one)
                session = session_queue[0]
                if session.last_contact_time + session.timeout > time():
                    # fail as timeout
                    session_queue.popleft()
                    self._session_dict[key] = session_queue
                    if session.failure_callback is not None:
                        session.failure_callback(session.file_name, 0, "Timeout")

                    # start next session in the queue
                    if not session_queue:
                        del self._session_dict[key]
                        return

                    session = session_queue[0]
                    session.next_func()

    def data_came_in(self, addr, data):
        """ The callback function that the RawServer will call when there is incoming data.
        :param addr: The (IP, port) address tuple of the sender.
        :param data: The data received.
        """
        ip, port = addr
        self._logger.debug(u"GOT packet [%s] from %s:%s", len(data), ip, port)

        # decode the packet
        try:
            packet = decode_packet(data)
        except InvalidPacketException as e:
            self._logger.error(u"Invalid packet from [%s:%s], packet=[%s], error=%s",
                               ip, port, hexlify(data), e)
            return

        # a new request
        if packet['opcode'] in (OPCODE_RRQ, OPCODE_WRQ):
            self._handle_new_request(ip, port, packet)

        # a response
        else:
            with self._session_lock:
                session_queue = self._session_dict.get(addr, None)
            if not session_queue:
                self._logger.error(u"Got empty session list for %s:%s", ip, port)
                return

            session = session_queue[0]
            self._process_packet(session, packet)

            if not session.is_done and not session.is_failed:
                return

            # remove this session from list and start the next one
            with self._session_lock:
                session_queue.popleft()
                self._session_dict[addr] = session_queue
                if session_queue:
                    self._logger.debug(u"Start the next session %s", session)
                    session_queue[0].next_func()
                else:
                    del self._session_dict[addr]

            # call callbacks
            if session.is_done and session.success_callback is not None:
                session.success_callback(session.file_data)
            elif session.is_failed and session.failure_callback is not None:
                session.failure_callback(session.file_data)

    def _handle_new_request(self, ip, port, packet):
        """ Handles a new request.
        :param ip:      The IP of the client.
        :param port:    The port of the client.
        :param packet:  The packet.
        """
        if packet['opcode'] != OPCODE_RRQ:
            self._logger.error(u"Unexpected request from %s:%s, opcode=%s: packet=%s",
                               ip, port, packet['opcode'], repr(packet))
            return
        if 'options' not in packet:
            self._logger.error(u"No 'options' in request from %s:%s, opcode=%s, packet=%s",
                               ip, port, packet['opcode'], repr(packet))
            return
        if 'blksize' not in packet['options'] or 'timeout' not in packet['options']:
            self._logger.error(u"No 'blksize' or 'timeout' not in 'options' from %s:%s, opcode=%s, packet=%s",
                               ip, port, packet['opcode'], repr(packet))
            return

        file_name = packet['file_name'].decode('utf8')
        block_size = packet['options']['blksize']
        timeout = packet['options']['timeout']

        # read the file/directory into memory
        if file_name.startswith(DIR_PREFIX):
            file_data, file_size = self._load_directory(ip, port, file_name)
        else:
            file_data, file_size = self._load_file(ip, port, file_name)

        with self._session_lock:
            # create a session object
            session = Session(False, (ip, port), packet['opcode'], file_name, file_data, file_size,
                              block_size=block_size, timeout=timeout)

            if (ip, port) not in self._session_dict:
                self._session_dict[(ip, port)] = deque()
            self._session_dict[(ip, port)].append(session)

            # if this session is the first one, we handle it. Otherwise, we delay it.
            if session == self._session_dict[(ip, port)][0]:
                # send back OACK now
                self._send_oack_packet(session)
            else:
                # save the next function that this session should call so that we can do it later.
                self.next_func = lambda s = session: self._send_oack_packet(s)

    def _load_file(self, ip, port, file_name, file_path=None):
        """ Loads a file into memory.
        :param file_name: The path of the file.
        """
        # the _load_directory also uses this method to load zip file.
        if file_path is None:
            file_path = os.path.join(self.root_dir, file_name)

        # check if file exists
        if not os.path.exists(file_path):
            msg = u"file doesn't exist: %s" % file_path
            self._logger.warn(u"[READ %s:%s] %s", ip, port, msg)
            # TODO: send back error
            raise OSError(msg)
        elif not os.path.isfile(file_path):
            msg = u"not a file: %s" % file_path
            self._logger.warn(u"[READ %s:%s] %s", ip, port, msg)
            # TODO: send back error
            raise OSError(msg)

        # read the file into memory
        f = None
        try:
            f = open(file_path, 'rb')
            file_data = f.read()
        except (OSError, IOError) as e:
            self._logger.error(u"[READ %s:%s] failed to read file [%s]: %s", ip, port, file_path, e)
            # TODO: send back error
            raise e
        finally:
            if f is not None:
                f.close()
        file_size = len(file_data)
        return file_data, file_size

    def _load_directory(self, ip, port, file_name):
        """ Loads a directory and all files, and compress using gzip to transfer.
        :param file_name: The directory name.
        """
        dir_name = file_name.split(DIR_SEPARATOR, 1)[1]
        dir_path = os.path.join(self.root_dir, dir_name)

        # check if file exists
        if not os.path.exists(dir_path):
            msg = u"directory doesn't exist: %s" % file_name
            self._logger.warn(u"[READ %s:%s] %s", ip, port, msg)
            # TODO: send back error
            raise OSError(msg)
        elif not os.path.isdir(dir_path):
            msg = u"not a directory: %s" % file_name
            self._logger.warn(u"[READ %s:%s] %s", ip, port, msg)
            # TODO: send back error
            raise OSError(msg)

        # create a temporary gzip file and compress the whole directory
        tmpfile_no, tmpfile_path = mkstemp(suffix=u"_tribler_tftpdir", prefix=u"tmp_")
        os.close(tmpfile_no)

        tar_file = TarFile.open(tmpfile_path, "w")
        tar_file.add(dir_path, arcname=dir_name, recursive=True)
        tar_file.close()

        # load the zip file as binary
        return self._load_file(ip, port, tmpfile_path)

    def _get_next_data(self, session):
        """ Gets the next block of data to be uploaded. This method is only used for data uploading.
        :return The data to transfer.
        """
        start_idx = session.block_number * session.block_size
        end_idx = start_idx + self.block_size
        data = session.file_data[start_idx:end_idx]
        session.block_number += 1

        # check if we are done
        if session.last_read_count is None:
            session.last_read_count = len(data)

        if session.last_read_count < session.block_size:
            session.is_done = True
        session.last_read_count = len(data)

        return data

    def _process_packet(self, session, packet):
        """ processes an incoming packet.
        :param packet: The incoming packet dictionary.
        """
        # check if it is an ERROR packet
        if packet['opcode'] == OPCODE_ERROR:
            self._logger.error(u"%s got ERROR message: code = %s, msg = %s",
                               session, packet['error_code'], packet['error_msg'])
            self._handle_error(session, 0)  # Error
            return

        # client is the receiver, server is the sender
        if session.is_client:
            self._handle_packet_as_receiver(session, packet)
        else:
            self._handle_packet_as_sender(session, packet)

    def _handle_packet_as_receiver(self, session, packet):
        """ Processes an incoming packet as a receiver.
        :param packet: The incoming packet dictionary.
        """
        # check if it is an ERROR packet
        if packet['opcode'] == OPCODE_ERROR:
            self._logger.error(u"[RECV %s] Got ERROR message: code = %s, msg = %s",
                               session, packet['error_code'], packet['error_msg'])
            self._handle_error(session, 0)  # Error
            return

        # if this is the first packet, check OACK
        if packet['opcode'] == OPCODE_OACK:
            if session.last_received_packet is None:
                if session.request == OPCODE_RRQ:
                    # send ACK
                    self._send_ack_packet(session, session.block_number)
                    session.block_number += 1
                    session.file_data = ""

            else:
                self._logger.error(u"[RECV %s]: Got OPCODE %s which is not expected", session, packet['opcode'])
                self._handle_error(session, 4)  # illegal TFTP operation
            return

        # expect a DATA
        if packet['opcode'] != OPCODE_DATA:
            self._logger.error(u"[RECV %s] Got OPCODE %s while expecting %s", session, packet['opcode'], OPCODE_DATA)
            self._handle_error(session, 4)  # illegal TFTP operation
            return

        # check block_number
        if packet['block_number'] != session.block_number:
            self._logger.error(u"[RECV %s] Got ACK with block# %s while expecting %s",
                               session, packet['block_number'], session.block_number)
            self._handle_error(session, 0)  # TODO: check error code
            return

        # save data
        session.file_data += packet['data']
        self._send_ack_packet(session, session.block_number)
        session.block_number += 1

        # check if it is the end
        if len(packet['data']) < session.block_size:
            session.is_done = True
            self._logger.info(u"[RECV %s] transfer finished", self)

    def _handle_packet_as_sender(self, session, packet):
        """ Processes an incoming packet as a sender.
        :param packet: The incoming packet dictionary.
        """
        # expect an ACK packet
        if packet['opcode'] != OPCODE_ACK:
            self._logger.error(u"[SEND %s] got OPCODE(%s) while expecting %s", session, packet['opcode'], OPCODE_ACK)
            self._handle_error(session, 4)  # illegal TFTP operation
            return

        # check block number
        if packet['block_number'] != session.block_number:
            self._logger.error(u"[SEND %s] got ACK with block# %s while expecting %s",
                               session, packet['block_number'], session.block_number)
            self._handle_error(session, 0)  # TODO: check error code
            return

        data = self._get_next_data(session)
        if session.is_done:
            self._logger.info(u"[SEND %s] finished", session)
        else:
            # send DATA
            self._send_data_packet(session, session.block_number, data)

    def _handle_error(self, session, error_code, error_msg=""):
        """ Handles an error during packet processing.
        :param error_code: The error code.
        """
        session.is_failed = True
        msg = ERROR_DICT.get(error_code, error_msg)
        self._send_error_packet(session, error_code, msg)

    def _send_packet(self, session, packet):
        packet_buff = encode_packet(packet)
        extra_msg = u" block_number = %s" % packet['block_number'] if packet.get('block_number') is not None else ""
        extra_msg += u" block_size = %s" % len(packet['data']) if packet.get('data') is not None else ""

        self._logger.debug(u"SEND OP[%s] -> %s %s %s",
                           packet['opcode'], session.address[0], session.address[1], extra_msg)
        self.endpoint.send_packet(Candidate(session.address, False), packet_buff, prefix=self.prefix)

        # update information
        session.last_contact_time = time()
        session.last_sent_packet = packet

    def _send_request_packet(self, session):
        assert session.request == OPCODE_RRQ, u"Invalid request_opcode %s" % repr(session.request)

        packet = {'opcode': session.request,
                  'file_name': session.file_name.encode('utf8'),
                  'options': {'blksize': session.block_size,
                              'timeout': session.timeout,
                              }}
        self._send_packet(session, packet)

    def _send_data_packet(self, session, block_number, data):
        packet = {'opcode': OPCODE_DATA,
                  'block_number': block_number,
                  'data': data}
        self._send_packet(session, packet)

    def _send_ack_packet(self, session, block_number):
        packet = {'opcode': OPCODE_ACK,
                  'block_number': block_number}
        self._send_packet(session, packet)

    def _send_error_packet(self, session, error_code, error_msg):
        packet = {'opcode': OPCODE_ERROR,
                  'error_code': error_code,
                  'error_msg': error_msg
                  }
        self._send_packet(session, packet)

    def _send_oack_packet(self, session):
        packet = {'opcode': OPCODE_OACK,
                  'block_number': session.block_number,
                  'options': {'blksize': session.block_size,
                              'timeout': session.timeout,
                              'tsize': session.file_size,
                              }}
        self._send_packet(session, packet)
