# Copyright 2019 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Define the enclosure interface for Mark II devices."""
import json
import time
from threading import Timer
from websocket import WebSocketApp

from mycroft.client.enclosure.base import Enclosure
from mycroft.messagebus.message import Message
from mycroft.util import create_daemon, connected
from mycroft.util.log import LOG

import threading

class EnclosureMark2(Enclosure):
    def __init__(self):
        LOG.info('** Initialize Enclosure Mark2 **')
        super().__init__()
        self.display_bus_client = None
        self.finished_loading = False
        self.active_screen = 'loading'
        self.paused_screen = None
        self.is_pairing = False
        self.active_until_stopped = None
        
        LOG.info('** Enclosure Mark2 Initalized **')
        self.bus.once('mycroft.skills.trained', self.is_device_ready)

    def is_device_ready(self, message):
        is_ready = False
        # Bus service assumed to be alive if messages sent and received
        # Enclosure assumed to be alive if this method is running
        services = {'audio': False, 'speech': False, 'skills': False}
        start = time.monotonic()
        while not is_ready:
            is_ready = self.check_services_ready(services)
            if is_ready:
                break
            elif time.monotonic() - start >= 60:
                raise Exception('Timeout waiting for services start.')
            else:
                time.sleep(3)

        if is_ready:
            LOG.info("All Mycroft Services have reported ready.")
            if connected():
                self.bus.emit(Message('mycroft.ready'))
            else:
                self.bus.emit(Message('mycroft.wifi.setup'))

        return is_ready

    def check_services_ready(self, services):
        """Report if all specified services are ready.

        services (iterable): service names to check.
        """
        for ser in services:
            services[ser] = False
            response = self.bus.wait_for_response(Message(
                'mycroft.{}.is_ready'.format(ser)))
            if response and response.data['status']:
                services[ser] = True
        return all([services[ser] for ser in services])
