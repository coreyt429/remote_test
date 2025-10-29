"""
This module contains classes for working with BroadWorks OCI
This version is stripped down to do a minimal login test without serverinfo
"""
import sys
import socket
import hashlib
import random
import html
import xml.etree.ElementTree as ET
import re
import logging
# from bw.util import Util
# from bw.response import OciResponse
# from bw.enterprise import Enterprise
# from bw.group import Group
# from bw.user import User
# import serverinfo



class Oci():
    """
    Class for managing OCS connections and sending/receiving OCI messages
    """

    def __init__(self, **kwargs):
        """
        Init function
        """
        # Create a logger for this class
        self.logger = logging.getLogger(self.__class__.__name__)
        # self.util = Util(self)
        # set default timeout
        self.timeout = kwargs.get('timeout', 180)
        # Configure the logger for this class
        self.logger.setLevel(kwargs.get('log_level', logging.WARNING))
        # save cluster
        self.cluster = kwargs.get('cluster', None)
        self.enterprise = None
        self.group = None
        # placeholder for socket
        self.sock = None
        self.ocs = None
        self.user_name = None
        self.password = None
        self.session_id = None
        self.last_request = None
        self.last_response = None
        self.type = 'AS'

    def __str__(self):

        return f"{self.cluster}"





    ## Core functions
    def connect(self):
        if not self.ocs:
            self.logger.warning("Cowardly refusing to connect to undefined host.\n")
            # Add logging or stack trace if needed
            return None

        # random seed for session_id
        random_number = random.randint(0, 10000)
        # generate session_id
        self.session_id = hashlib.md5(
                hashlib.sha1(str(random_number).encode()).digest()
            ).hexdigest()
        try:
            # Create a socket connection
            self.sock = socket.create_connection((self.ocs, 2208), timeout=2)
            # Build and send Authentication request
            command = 'AuthenticationRequest'
            params = [{'userId': self.user_name}]
            response = self.get_cmd_raw(command,params,2)
            if not response or 'Error' in response:
                self.logger.warning("Login failure: \n%s\n%s\n%s\n", command, self.ocs, response)
                return None
            # Parse nonce and password algorithm from response
            # This is a placeholder; actual parsing depends on response format
            nonce, pa = self.parse_response(response)
            # Hash the password
            password_sha = hashlib.sha1(self.password.encode()).hexdigest()
            if pa == 'MD5':
                phash = hashlib.md5(f"{nonce}:{password_sha}".encode()).hexdigest()
            else:
                phash = hashlib.sha1(f"{nonce}:{password_sha}".encode()).hexdigest()
            # build and send login request
            command = 'LoginRequest14sp4'
            params = [{'userId': self.user_name},{'signedPassword': phash}]
            response = self.get_cmd_raw(command,params,2)
            # print(response)
            if response is None or 'Error' in response or response == '':
                self.logger.warning("OCI::connect({host},{username}): Login failure: \n{response}\n",
                     self.ocs, self.user_name, response)
                # Optionally, log the message or perform other actions as needed
                return None
            return True
        except (socket.timeout, socket.error) as error_message:
            # Handle connection errors
            print(f"Could not create socket: {error_message}")
            return False

    def parse_response(self, xml_response):
        """
        Fuction to parse XML responses
        used my get_cmd_xml
        deprecated by get_cmd
        """
        # Parse the XML response
        root = ET.fromstring(xml_response)
        # Find the nonce and passwordAlgorithm elements
        nonce = root.find('.//nonce')
        password_algorithm = root.find('.//passwordAlgorithm')
        # Extract and return the text content of these elements
        return nonce.text if nonce is not None else None, \
               password_algorithm.text if password_algorithm is not None else None

    def get_cmd_raw(self, cmd, p, timeout=None):
        """
        Function to build and send an OCI request
        """
        # build XML request
        request = self.wrap_command(self.build_command(cmd,p,1))
        # store as last_request
        self.last_request = request
        # send request to the OCS server
        self.send_request(request)
        # get response from OCS server
        response = self.get_response(timeout)
        # store last response
        self.last_response = response
        return response

    def get_cmd_xml(self, cmd, p, xml_options=None):
        """
        Functon to get oci response and convert to xml tree
        deprecated by get_cmd
        """
        response = self.get_cmd_raw(cmd, p)
        if xml_options is None:
            xml_options = {}
        try:
            # Remove namespaces
            response = re.sub(' xmlns="[^"]+"', '', response, count=1)
            # Trim leading whitespace
            response = response.lstrip()
            # Parse the XML
            result = ET.fromstring(response)
        except ET.ParseError as error_message:
            self.logger.warning("XML parsing error: %S", error_message)
            self.logger.warning(response)
            sys.exit()
        return result

    def build_params(self, params, delete=True):
        """
        Function to generate XML from a parameter structure
        """
        retval = ''
        # walk params
        for param in params:
            # walk key/value pairs
            for key, value in param.items():
                # if dict, recurse
                if isinstance(value, dict):
                    retval += f"<{key}>\n"
                    retval += self.build_params([value], delete)
                    retval += f"</{key}>\n"
                # if list, recurse
                elif isinstance(value, list):
                    retval += f"<{key}>\n"
                    retval += self.build_params(value, delete)
                    retval += f"</{key}>\n"
                # if empty set to nil
                elif value == '':
                    retval += f"<{key} xsi:nil=\"true\"/>\n"
                # else add key/value
                else:
                    escaped_value = html.escape(str(value))
                    retval += f"<{key}>{escaped_value}</{key}>\n"
        # if delete flag is set clear parameters
        if delete:
            params.clear()
        # return xml
        return retval

    def build_command(self, command, params, delete=True):
        """
        Function to build OCI Request
        """
        # Base XML command structure
        retval = ' '.join([
                f'<command xsi:type="{command}"',
                'xmlns=""',
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>'
            ]
        )
        # build params
        if params is not None:
            param_str = self.build_params(params, delete)
            param_str = param_str.strip()
            retval = f"<command xsi:type=\"{command}\" xmlns=\"\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\">\n{param_str}\n</command>"
        # return XML
        return retval

    def wrap_command(self, command):
        """
        Function to wrap a command for sending
        """
        # Remove trailing whitespace/newlines from command
        command = command.strip()
        # Constructing the XML string
        retval = f"""<?xml version="1.0" encoding="ISO-8859-1"?>
    <BroadsoftDocument protocol="OCI" xmlns="C">
    <sessionId xmlns="">{self.session_id}</sessionId>
    {command}
    </BroadsoftDocument>

    """
        # return XML
        return retval

    def send_request(self, mesg):
        """
        Function to send requests to the OCS server
        """
        self.logger.debug("send_request(%s)", mesg)
        # do we have a socket?
        if self.sock:
            try:
                # Sending the message to the socket
                # Ensure the message is a byte string
                if isinstance(mesg, str):
                    mesg = mesg.encode('utf-8')
                self.sock.sendall(mesg)
            except Exception as error_message:
                # Handle any exceptions (e.g., socket errors)
                self.logger.warning("send_request(): Error sending message: %s", error_message)
        else:
            self.logger.warning("send_request(): No Socket")

    def get_response(self, timeout=None):
        """
        Function to get response from OCS server
        """
        if not timeout:
            timeout = self.timeout
        # do we have a socket?
        if self.sock is None:
            self.logger.warning("get_response(): No Socket")
            return None
        # set timeout
        self.sock.settimeout(timeout)
        # response byte structure
        response_bytes = bytearray()
        try:
            # Receive data until the end marker is found
            while b'/BroadsoftDocument>' not in response_bytes:
                received = self.sock.recv(1024)  # Buffer size of 1024 bytes
                if not received:
                    # No more data is coming through the socket
                    break
                response_bytes += received
        except socket.timeout:
            self.logger.warning("get_response(): Socket timed out")
        except Exception as error_message:
            self.logger.warning("get_response(): Error receiving response:", error_message)
            return None
        # Try decoding with UTF-8 first, then fall back to ISO-8859-1 if necessary
        try:
            response = response_bytes.decode('utf-8')
        except UnicodeDecodeError:
            response = response_bytes.decode('iso-8859-1')
        return response
