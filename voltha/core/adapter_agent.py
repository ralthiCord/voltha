#
# Copyright 2017 the original author or authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Agent to play gateway between CORE and an individual adapter.
"""
from uuid import uuid4

import arrow
import structlog
from google.protobuf.json_format import MessageToJson
from scapy.packet import Packet
from twisted.internet.defer import inlineCallbacks, returnValue
from zope.interface import implementer

from common.event_bus import EventBusClient
from common.frameio.frameio import hexify
from voltha.adapters.interface import IAdapterAgent
from voltha.protos import third_party
from voltha.core.flow_decomposer import OUTPUT
from voltha.protos.device_pb2 import Device, Port, PmConfigs
from voltha.protos.events_pb2 import AlarmEvent, AlarmEventType, \
    AlarmEventSeverity, AlarmEventState, AlarmEventCategory
from voltha.protos.events_pb2 import KpiEvent
from voltha.protos.voltha_pb2 import DeviceGroup, LogicalDevice, \
    LogicalPort, AdminState, OperStatus, AlarmFilterRuleKey
from voltha.registry import registry


@implementer(IAdapterAgent)
class AdapterAgent(object):
    """
    Gate-keeper between CORE and device adapters.

    On one side it interacts with Core's internal model and update/dispatch
    mechanisms.

    On the other side, it interacts with the adapters standard interface as
    defined in
    """

    def __init__(self, adapter_name, adapter_cls):
        self.adapter_name = adapter_name
        self.adapter_cls = adapter_cls
        self.core = registry('core')
        self.adapter = None
        self.adapter_node_proxy = None
        self.root_proxy = self.core.get_proxy('/')
        self._rx_event_subscriptions = {}
        self._tx_event_subscriptions = {}
        self.event_bus = EventBusClient()
        self.packet_out_subscription = None
        self.log = structlog.get_logger(adapter_name=adapter_name)

    @inlineCallbacks
    def start(self):
        self.log.debug('starting')
        config = self._get_adapter_config()  # this may be None
        try:
            adapter = self.adapter_cls(self, config)
            yield adapter.start()
            self.adapter = adapter
            self.adapter_node_proxy = self._update_adapter_node()
            self._update_device_types()
        except Exception, e:
            self.log.exception(e)
        self.log.info('started')
        returnValue(self)

    @inlineCallbacks
    def stop(self):
        self.log.debug('stopping')
        if self.adapter is not None:
            yield self.adapter.stop()
            self.adapter = None
        self.log.info('stopped')

    def _get_adapter_config(self):
        """
        Opportunistically load persisted adapter configuration.
        Return None if no configuration exists yet.
        """
        proxy = self.core.get_proxy('/')
        try:
            config = proxy.get('/adapters/' + self.adapter_name)
            return config
        except KeyError:
            return None

    def _update_adapter_node(self):
        """
        Creates or updates the adapter node object based on self
        description from the adapter.
        """

        adapter_desc = self.adapter.adapter_descriptor()
        assert adapter_desc.id == self.adapter_name
        path = self._make_up_to_date(
            '/adapters', self.adapter_name, adapter_desc)
        return self.core.get_proxy(path)

    def _update_device_types(self):
        """
        Make sure device types are registered in Core
        """
        device_types = self.adapter.device_types()
        for device_type in device_types.items:
            key = device_type.id
            self._make_up_to_date('/device_types', key, device_type)

    def _make_up_to_date(self, container_path, key, data):
        full_path = container_path + '/' + str(key)
        root_proxy = self.core.get_proxy('/')
        try:
            root_proxy.get(full_path)
            root_proxy.update(full_path, data)
        except KeyError:
            root_proxy.add(container_path, data)
        return full_path

    def _remove_node(self, container_path, key):
        """
        Remove a node from the data model
        :param container_path: path to node
        :param key: node
        :return: None
        """
        full_path = container_path + '/' + str(key)
        root_proxy = self.core.get_proxy('/')
        try:
            root_proxy.get(full_path)
            root_proxy.remove(full_path)
        except KeyError:
            # Node does not exist
            pass

    # ~~~~~~~~~~~~~~~~~~~~~ Core-Facing Service ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def adopt_device(self, device):
        return self.adapter.adopt_device(device)

    def abandon_device(self, device):
        return self.adapter.abandon_device(device)

    def disable_device(self, device):
        return self.adapter.disable_device(device)

    def reenable_device(self, device):
        return self.adapter.reenable_device(device)

    def reboot_device(self, device):
        return self.adapter.reboot_device(device)

    def delete_device(self, device):
        return self.adapter.delete_device(device)

    def get_device_details(self, device):
        return self.adapter.get_device_details(device)

    def update_flows_bulk(self, device, flows, groups):
        return self.adapter.update_flows_bulk(device, flows, groups)

    def update_flows_incrementally(self, device, flow_changes, group_changes):
        return self.update_flows_incrementally(
            device, flow_changes, group_changes)

    # def update_pm_collection(self, device, pm_collection_config):
    #    return self.adapter.update_pm_collection(device, pm_collection_config)


    # ~~~~~~~~~~~~~~~~~~~ Adapter-Facing Service ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def get_device(self, device_id):
        return self.root_proxy.get('/devices/{}'.format(device_id))

    def add_device(self, device):
        assert isinstance(device, Device)
        self._make_up_to_date('/devices', device.id, device)

        # Ultimately, assign devices to device grpups.
        # see https://jira.opencord.org/browse/CORD-838

        dg = DeviceGroup(id='1')
        self._make_up_to_date('/device_groups', dg.id, dg)

        # add device to device group
        # see https://jira.opencord.org/browse/CORD-838

    def update_device(self, device):
        assert isinstance(device, Device)

        # we run the update through the device_agent so that the change
        # does not loop back to the adapter unnecessarily
        device_agent = self.core.get_device_agent(device.id)
        device_agent.update_device(device)

    def update_device_pm_config(self, device_pm_config, init=False):
        assert isinstance(device_pm_config, PmConfigs)

        # we run the update through the device_agent so that the change
        # does not loop back to the adapter unnecessarily
        device_agent = self.core.get_device_agent(device_pm_config.id)
        device_agent.update_device_pm_config(device_pm_config, init)

    def update_adapter_pm_config(self, device_id, device_pm_config):
        device = self.get_device(device_id)
        self.adapter.update_pm_config(device, device_pm_config)

    def _add_peer_reference(self, device_id, port):
        # for referential integrity, add/augment references
        port.device_id = device_id
        me_as_peer = Port.PeerPort(device_id=device_id, port_no=port.port_no)
        for peer in port.peers:
            peer_port_path = '/devices/{}/ports/{}'.format(
                peer.device_id, peer.port_no)
            peer_port = self.root_proxy.get(peer_port_path)
            if me_as_peer not in peer_port.peers:
                new = peer_port.peers.add()
                new.CopyFrom(me_as_peer)
            self.root_proxy.update(peer_port_path, peer_port)

    def _del_peer_reference(self, device_id, port):
        me_as_peer = Port.PeerPort(device_id=device_id, port_no=port.port_no)
        for peer in port.peers:
            peer_port_path = '/devices/{}/ports/{}'.format(
                peer.device_id, peer.port_no)
            peer_port = self.root_proxy.get(peer_port_path)
            if me_as_peer in peer_port.peers:
                peer_port.peers.remove(me_as_peer)
            self.root_proxy.update(peer_port_path, peer_port)

    def add_port(self, device_id, port):
        assert isinstance(port, Port)

        # for referential integrity, add/augment references
        self._add_peer_reference(device_id, port)

        # Add port
        self._make_up_to_date('/devices/{}/ports'.format(device_id),
                              port.port_no, port)

    def disable_all_ports(self, device_id):
        """
        Disable all ports on that device, i.e. change the admin status to
        disable and operational status to UNKNOWN.
        :param device_id: device id
        :return: None
        """

        # get all device ports
        ports = self.root_proxy.get('/devices/{}/ports'.format(device_id))
        for port in ports:
            port.admin_state = AdminState.DISABLED
            port.oper_status = OperStatus.UNKNOWN
            self._make_up_to_date('/devices/{}/ports'.format(device_id),
                                  port.port_no, port)

    def enable_all_ports(self, device_id):
        """
        Re-enable all ports on that device, i.e. change the admin status to
        enabled and operational status to ACTIVE
        :param device_id: device id
        :return: None
        """

        # get all device ports
        ports = self.root_proxy.get('/devices/{}/ports'.format(device_id))
        for port in ports:
            port.admin_state = AdminState.ENABLED
            port.oper_status = OperStatus.ACTIVE
            self._make_up_to_date('/devices/{}/ports'.format(device_id),
                                  port.port_no, port)

    def delete_all_peer_references(self, device_id):
        """
        Remove all peer port references for that device
        :param device_id: device_id of device
        :return: None
        """
        ports = self.root_proxy.get('/devices/{}/ports'.format(device_id))
        for port in ports:
            port_path = '/devices/{}/ports/{}'.format(device_id, port.port_no)
            for peer in port.peers:
                port.peers.remove(peer)
            self.root_proxy.update(port_path, port)

    def delete_port_reference_from_parent(self, device_id, port):
        """
        Delete the port reference from the parent device
        :param device_id: id of device containing the port
        :param port: port to remove
        :return: None
        """
        assert isinstance(port, Port)
        self.log.info('delete-port-reference', device_id=device_id, port=port)
        self._del_peer_reference(device_id, port)

    def add_port_reference_to_parent(self, device_id, port):
        """
        Add the port reference to the parent device
        :param device_id: id of device containing the port
        :param port: port to add
        :return: None
        """
        assert isinstance(port, Port)
        self.log.info('add-port-reference', device_id=device_id, port=port)
        self._add_peer_reference(device_id, port)

    def _find_first_available_id(self):
        logical_devices = self.root_proxy.get('/logical_devices')
        existing_ids = set(ld.id for ld in logical_devices)
        existing_datapath_ids = set(ld.datapath_id for ld in logical_devices)
        i = 1
        while True:
            if i not in existing_datapath_ids and str(i) not in existing_ids:
                return i
            i += 1

    def get_logical_device(self, logical_device_id):
        return self.root_proxy.get('/logical_devices/{}'.format(
            logical_device_id))

    def get_logical_port(self, logical_device_id, port_id):
        return self.root_proxy.get('/logical_devices/{}/ports/{}'.format(
            logical_device_id, port_id))

    def create_logical_device(self, logical_device):
        assert isinstance(logical_device, LogicalDevice)

        if not logical_device.id:
            id = self._find_first_available_id()
            logical_device.id = str(id)
            logical_device.datapath_id = id

        self._make_up_to_date('/logical_devices',
                              logical_device.id, logical_device)

        # Keep a reference to the packet out subscription as it will be
        # referred during removal
        self.packet_out_subscription = self.event_bus.subscribe(
            topic='packet-out:{}'.format(logical_device.id),
            callback=lambda _, p: self.receive_packet_out(logical_device.id, p)
        )

        return logical_device

    def delete_logical_device(self, logical_device):
        """
        This will remove the logical device as well as all logical ports
        associated with it
        :param logical_device: The logical device to remove
        :return: None
        """
        assert isinstance(logical_device, LogicalDevice)

        # Remove packet out subscription
        self.event_bus.unsubscribe(self.packet_out_subscription)

        # Remove node from the data model - this will trigger the logical
        # device 'remove callbacks' as well as logical ports 'remove
        # callbacks' if present
        self._remove_node('/logical_devices', logical_device.id)

    def receive_packet_out(self, logical_device_id, ofp_packet_out):

        def get_port_out(opo):
            for action in opo.actions:
                if action.type == OUTPUT:
                    return action.output.port

        out_port = get_port_out(ofp_packet_out)
        frame = ofp_packet_out.data
        self.adapter.receive_packet_out(logical_device_id, out_port, frame)

    def add_logical_port(self, logical_device_id, port):
        assert isinstance(port, LogicalPort)
        self._make_up_to_date(
            '/logical_devices/{}/ports'.format(logical_device_id),
            port.id, port)

    def delete_logical_port(self, logical_device_id, port):
        assert isinstance(port, LogicalPort)
        self._remove_node('/logical_devices/{}/ports'.format(
            logical_device_id), port.id)

    def update_logical_port(self, logical_device_id, port):
        assert isinstance(port, LogicalPort)
        self.log.debug('update-logical-port',
                       logical_device_id=logical_device_id,
                       port=port)

        self._make_up_to_date(
            '/logical_devices/{}/ports'.format(logical_device_id),
            port.id, port)

    def child_device_detected(self,
                              parent_device_id,
                              parent_port_no,
                              child_device_type,
                              proxy_address,
                              **kw):
        # we create new ONU device objects and insert them into the config
        # TODO should we auto-enable the freshly created device? Probably.
        device = Device(
            id=uuid4().hex[:12],
            type=child_device_type,
            parent_id=parent_device_id,
            parent_port_no=parent_port_no,
            admin_state=AdminState.ENABLED,
            proxy_address=proxy_address,
            **kw
        )
        self._make_up_to_date(
            '/devices', device.id, device)

        topic = self._gen_tx_proxy_address_topic(proxy_address)
        self._tx_event_subscriptions[topic] = self.event_bus.subscribe(
            topic, lambda t, m: self._send_proxied_message(proxy_address, m))

    def remove_all_logical_ports(self, logical_device_id):
        """ Remove all logical ports from a given logical device"""
        ports = self.root_proxy.get('/logical_devices/{}/ports')
        for port in ports:
            self._remove_node('/logical_devices/{}/ports', port.id)

    def delete_all_child_devices(self, parent_device_id):
        """ Remove all ONUs from a given OLT """
        devices = self.root_proxy.get('/devices')
        children_ids = set(d.id for d in devices if d.parent_id == parent_device_id)
        self.log.debug('devices-to-delete',
                       parent_id=parent_device_id,
                       children_ids=children_ids)
        for child_id in children_ids:
            self._remove_node('/devices', child_id)

    def update_child_devices_state(self,
                                   parent_device_id,
                                   oper_status=None,
                                   connect_status=None,
                                   admin_state=None):
        """ Update status of all child devices """
        devices = self.root_proxy.get('/devices')
        children_ids = set(d.id for d in devices if d.parent_id == parent_device_id)
        self.log.debug('update-devices',
                       parent_id=parent_device_id,
                       children_ids=children_ids,
                       oper_status=oper_status,
                       connect_status=connect_status,
                       admin_state=admin_state)

        for child_id in children_ids:
            device = self.get_device(child_id)
            if oper_status:
                device.oper_status = oper_status
            if connect_status:
                device.connect_status = connect_status
            if admin_state:
                device.admin_state = admin_state
            self._make_up_to_date(
                '/devices', device.id, device)

    def _gen_rx_proxy_address_topic(self, proxy_address):
        """Generate unique topic name specific to this proxy address for rx"""
        topic = 'rx:' + MessageToJson(proxy_address)
        return topic

    def _gen_tx_proxy_address_topic(self, proxy_address):
        """Generate unique topic name specific to this proxy address for tx"""
        topic = 'tx:' + MessageToJson(proxy_address)
        return topic

    def register_for_proxied_messages(self, proxy_address):
        topic = self._gen_rx_proxy_address_topic(proxy_address)
        self._rx_event_subscriptions[topic] = self.event_bus.subscribe(
            topic,
            lambda t, m: self._receive_proxied_message(proxy_address, m))

    def unregister_for_proxied_messages(self, proxy_address):
        topic = self._gen_rx_proxy_address_topic(proxy_address)
        self.event_bus.unsubscribe(self._rx_event_subscriptions[topic])
        del self._rx_event_subscriptions[topic]

    def _receive_proxied_message(self, proxy_address, msg):
        self.adapter.receive_proxied_message(proxy_address, msg)

    def send_proxied_message(self, proxy_address, msg):
        topic = self._gen_tx_proxy_address_topic(proxy_address)
        self.event_bus.publish(topic, msg)

    def _send_proxied_message(self, proxy_address, msg):
        self.adapter.send_proxied_message(proxy_address, msg)

    def receive_proxied_message(self, proxy_address, msg):
        topic = self._gen_rx_proxy_address_topic(proxy_address)
        self.event_bus.publish(topic, msg)

    # ~~~~~~~~~~~~~~~~~~ Handling packet-in and packet-out ~~~~~~~~~~~~~~~~~~~~

    def send_packet_in(self, logical_device_id, logical_port_no, packet):
        self.log.debug('send-packet-in', logical_device_id=logical_device_id,
                       logical_port_no=logical_port_no, packet=hexify(packet))

        if isinstance(packet, Packet):
            packet = str(packet)

        topic = 'packet-in:' + logical_device_id
        self.event_bus.publish(topic, (logical_port_no, packet))

    # ~~~~~~~~~~~~~~~~~~~ Handling KPI metric submissions ~~~~~~~~~~~~~~~~~~~~~

    def submit_kpis(self, kpi_event_msg):
        try:
            assert isinstance(kpi_event_msg, KpiEvent)
            self.event_bus.publish('kpis', kpi_event_msg)
        except Exception as e:
            self.log.exception('failed-kpi-submission',
                               type=type(kpi_event_msg))

    # ~~~~~~~~~~~~~~~~~~~ Handle alarm submissions ~~~~~~~~~~~~~~~~~~~~~

    def create_alarm(self, id=None, resource_id=None, description=None,
                     raised_ts=0, changed_ts=0,
                     type=AlarmEventType.EQUIPMENT,
                     category=AlarmEventCategory.PON,
                     severity=AlarmEventSeverity.MINOR,
                     state=AlarmEventState.RAISED,
                     context=None):

        # Construct the ID if it is not provided
        if id == None:
            id = 'voltha.{}.{}'.format(self.adapter_name, resource_id)

        return AlarmEvent(
            id=id,
            resource_id=resource_id,
            type=type,
            category=category,
            severity=severity,
            state=state,
            description=description,
            reported_ts=arrow.utcnow().timestamp,
            raised_ts=raised_ts,
            changed_ts=changed_ts,
            context=context
        )

    def filter_alarm(self, device_id, alarm_event):
        alarm_filters = self.root_proxy.get('/alarm_filters')

        rule_values = {
            'id': alarm_event.id,
            'type': AlarmEventType.AlarmEventType.Name(alarm_event.type),
            'category': AlarmEventCategory.AlarmEventCategory.Name(alarm_event.category),
            'severity': AlarmEventSeverity.AlarmEventSeverity.Name(alarm_event.severity),
            'resource_id': alarm_event.resource_id,
            'device_id': device_id
        }

        for alarm_filter in alarm_filters:
            if alarm_filter.rules:
                exclude = True
                for rule in alarm_filter.rules:
                    self.log.debug("compare-alarm-event",
                                   key=AlarmFilterRuleKey.AlarmFilterRuleKey.Name(rule.key),
                                   actual=rule_values[AlarmFilterRuleKey.AlarmFilterRuleKey.Name(rule.key).lower()],
                                   expected=rule.value.lower())
                    exclude = exclude and \
                              (rule_values[AlarmFilterRuleKey.AlarmFilterRuleKey.Name(
                                  rule.key).lower()] == rule.value.lower())
                    if not exclude:
                        break

                if exclude:
                    self.log.info("filtered-alarm-event", alarm=alarm_event)
                    return True

        return False

    def submit_alarm(self, device_id, alarm_event_msg):
        try:
            assert isinstance(alarm_event_msg, AlarmEvent)
            if not self.filter_alarm(device_id, alarm_event_msg):
                self.event_bus.publish('alarms', alarm_event_msg)

        except Exception as e:
            self.log.exception('failed-alarm-submission',
                               type=type(alarm_event_msg))
