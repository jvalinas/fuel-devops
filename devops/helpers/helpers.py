#    Copyright 2013 - 2016 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# pylint: disable=redefined-builtin
from functools import reduce
# pylint: enable=redefined-builtin
import os
import socket
import time
from warnings import warn

import paramiko
# pylint: disable=import-error
from six.moves import http_client
from six.moves import xmlrpc_client
# pylint: enable=import-error


from devops.error import AuthenticationError
from devops.error import DevopsError
from devops.error import TimeoutError
from devops.helpers.ssh_client import SSHClient
from devops import logger
from devops.settings import KEYSTONE_CREDS
from devops.settings import SSH_CREDENTIALS
from devops.settings import SSH_SLAVE_CREDENTIALS


def get_free_port():
    for port in range(32000, 32100):
        if not tcp_ping('localhost', port):
            return port
    raise DevopsError('No free ports available')


def icmp_ping(host, timeout=1):
    """Run ICMP ping

    returns True if host is pingable
    False - otherwise.
    """
    return os.system(
        "ping -c 1 -W '%(timeout)d' '%(host)s' 1>/dev/null 2>&1" % {
            'host': host, 'timeout': timeout}) == 0


def tcp_ping_(host, port, timeout=None):
    s = socket.socket()
    if timeout:
        s.settimeout(timeout)
    s.connect((str(host), int(port)))
    s.close()


def _tcp_ping(*args, **kwargs):
    logger.warning('_tcp_ping is deprecated in favor of tcp_ping')
    warn('_tcp_ping is deprecated in favor of tcp_ping', DeprecationWarning)
    return tcp_ping_(*args, **kwargs)


def tcp_ping(host, port, timeout=None):
    """Run TCP ping

    returns True if TCP connection to specified host and port
    can be established
    False - otherwise.
    """
    try:
        tcp_ping_(host, port, timeout)
    except socket.error:
        return False
    return True


def wait(predicate, interval=5, timeout=60, timeout_msg="Waiting timed out"):
    """Wait until predicate will become True.

    returns number of seconds that is left or 0 if timeout is None.

    Options:

    interval - seconds between checks.

    timeout  - raise TimeoutError if predicate won't become True after
    this amount of seconds. 'None' disables timeout.

    timeout_msg - text of the TimeoutError

    """
    start_time = time.time()
    if not timeout:
        return predicate()
    while not predicate():
        if start_time + timeout < time.time():
            raise TimeoutError(timeout_msg)

        seconds_to_sleep = max(
            0,
            min(interval, start_time + timeout - time.time()))
        time.sleep(seconds_to_sleep)

    return timeout + start_time - time.time()


def wait_pass(raising_predicate, expected=Exception, interval=5, timeout=None):
    """Wait for successful return from predicate or expected exception"""
    start_time = time.time()
    while True:
        try:
            return raising_predicate()
        except expected:
            if timeout and start_time + timeout < time.time():
                raise
            time.sleep(interval)


def _wait(*args, **kwargs):
    logger.warning('_wait has been deprecated in favor of wait_pass')
    warn('_wait has been deprecated in favor of wait_pass', DeprecationWarning)
    return wait_pass(*args, **kwargs)


def http(host='localhost', port=80, method='GET', url='/', waited_code=200):
    try:
        conn = http_client.HTTPConnection(str(host), int(port))
        conn.request(method, url)
        res = conn.getresponse()

        return res.status == waited_code
    except Exception:
        return False


def get_private_keys(env):
    _ssh_keys = []
    admin_remote = get_admin_remote(env)
    for key_string in ['/root/.ssh/id_rsa',
                       '/root/.ssh/bootstrap.rsa']:
        if admin_remote.isfile(key_string):
            with admin_remote.open(key_string) as f:
                _ssh_keys.append(paramiko.RSAKey.from_private_key(f))
    return _ssh_keys


def get_admin_remote(env, login=SSH_CREDENTIALS['login'],
                     password=SSH_CREDENTIALS['password']):
    admin_ip = get_admin_ip(env)
    wait(lambda: tcp_ping(admin_ip, 22),
         timeout=180,
         timeout_msg=("Admin node {ip} is not accessible by SSH."
                      .format(ip=admin_ip)))
    return env.get_node(
        name='admin').remote(network_name=SSH_CREDENTIALS['admin_network'],
                             login=login,
                             password=password)


def get_node_remote(env, node_name, login=SSH_SLAVE_CREDENTIALS['login'],
                    password=SSH_SLAVE_CREDENTIALS['password']):
    ip = get_slave_ip(env, env.get_node(
        name=node_name).interfaces[0].mac_address)
    wait(lambda: tcp_ping(ip, 22), timeout=180,
         timeout_msg="Node {ip} is not accessible by SSH.".format(ip=ip))
    return SSHClient(ip,
                     username=login,
                     password=password,
                     private_keys=get_private_keys(env))


def get_admin_ip(env):
    return env.get_node(name='admin').get_ip_address_by_network_name('admin')


def get_slave_ip(env, node_mac_address):
    with get_admin_remote(env) as remote:
        ip = remote.execute(
            "KEYSTONE_USER={user} KEYSTONE_PASS={passwd} "
            "fuel nodes --node-id {mac} | awk -F'|' "
            "'END{{gsub(\" \", \"\", $5); print $5}}'".format(
                user=KEYSTONE_CREDS['username'],
                passwd=KEYSTONE_CREDS['password'],
                mac=node_mac_address))['stdout']
    return ip[0].rstrip()


def get_keys(ip, mask, gw, hostname, nat_interface, dns1, showmenu,
             build_images, centos_version=7, static_interface='enp0s3'):
    if centos_version < 7:
        ip_format = ' ip={ip}'
    else:
        ip_format = ' ip={ip}::{gw}:{mask}:{hostname}:{static_interface}:none'

    return '\n'.join([
        '<Wait>',
        '<Esc>',
        '<Wait>',
        'vmlinuz initrd=initrd.img ks=cdrom:/ks.cfg',
        ip_format,
        ' netmask={mask}'
        ' gw={gw}'
        ' dns1={dns1}',
        ' nameserver={dns1}',
        ' hostname={hostname}',
        ' dhcp_interface={nat_interface}',
        ' showmenu={showmenu}',
        ' build_images={build_images}',
        ' <Enter>',
        ''
    ]).format(
        ip=ip,
        mask=mask,
        gw=gw,
        hostname=hostname,
        nat_interface=nat_interface,
        dns1=dns1,
        showmenu=showmenu,
        build_images=build_images,
        static_interface=static_interface
    )


class KeyPolicy(paramiko.WarningPolicy):
    def __init__(self):
        warn(
            'devops.helpers.KeyPolicy is deprecated '
            'and will be removed soon', DeprecationWarning)
        logger.warning(
            'devops.helpers.KeyPolicy is deprecated '
            'and will be removed soon'
        )
        super(KeyPolicy, self).__init__()

    def missing_host_key(self, client, hostname, key):
        return


def ssh(*args, **kwargs):
    warn(
        'devops.helpers.ssh is deprecated '
        'and will be removed soon', DeprecationWarning)
    return SSHClient(*args, **kwargs)


def xmlrpctoken(uri, login, password):
    server = xmlrpc_client.Server(uri)
    try:
        return server.login(login, password)
    except Exception:
        raise AuthenticationError("Error occurred while login process")


def xmlrpcmethod(uri, method):
    server = xmlrpc_client.Server(uri)
    try:
        return getattr(server, method)
    except Exception:
        raise AttributeError("Error occurred while getting server method")


def generate_mac():
    return "64:{0:02x}:{1:02x}:{2:02x}:{3:02x}:{4:02x}".format(
        *bytearray(os.urandom(5)))


def get_file_size(path):
    """Get size of file-like object

    :type path: str
    :rtype : int
    """

    return os.stat(path).st_size


def _get_file_size(*args, **kwargs):
    logger.warning(
        '_get_file_size has been deprecated in favor of get_file_size')
    warn(
        '_get_file_size has been deprecated in favor of get_file_size',
        DeprecationWarning)
    return get_file_size(*args, **kwargs)


def deepgetattr(obj, attr, default=None, splitter='.', do_raise=False):
    """Recurses through an attribute chain to get the ultimate value.

    :type obj: object
    :param obj: object instance to get attribute from
    :type attr: str
    :param attr: attributes joined by some symbol. e.g. 'a.b.c.d'
    :type default: any
    :param default: default value (returned only in case of
                    AttributeError)
    :type splitter: str
    :param splitter: one or more symbols to be used to split attr
                     parameter
    :type do_raise: bool
    :param do_raise: if True then instead of returning default value
                     AttributeError will be raised

    """
    try:
        return reduce(getattr, attr.split(splitter), obj)
    except AttributeError:
        if do_raise:
            raise
        return default


def underscored(*args):
    """Joins multiple strings using uderscore symbol.

       Skips empty strings.
    """
    return '_'.join(filter(bool, list(args)))


def _underscored(*args):
    logger.warning(
        '_underscored has been deprecated in favor of underscored')
    warn(
        '_underscored has been deprecated in favor of underscored',
        DeprecationWarning)
    return underscored(*args)
