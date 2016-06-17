# Copyright 2016 Mycroft AI, Inc.
#
# This file is part of Mycroft Core.
#
# Mycroft Core is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Mycroft Core is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Mycroft Core.  If not, see <http://www.gnu.org/licenses/>.


import json
from os.path import exists

from mycroft.configuration import USER_THIRD_PARTY_SKILLS_DIR
from mycroft.messagebus.client.ws import WebsocketClient
from mycroft.skills.core import load_skills, THIRD_PARTY_SKILLS_DIR
from mycroft.util.log import getLogger
logger = getLogger("Skills")

__author__ = 'seanfitz'

client = None


def load_skills_callback():
    global client
    load_skills(client)
    if exists(THIRD_PARTY_SKILLS_DIR):
        load_skills(client, skills_root=THIRD_PARTY_SKILLS_DIR)

    if exists(USER_THIRD_PARTY_SKILLS_DIR):
        load_skills(client, skills_root=USER_THIRD_PARTY_SKILLS_DIR)


def connect():
    global client
    client.run_forever()


def main():
    global client
    client = WebsocketClient()

    def echo(message):
        try:
            _message = json.loads(message)

            if _message.get("message_type") == "registration":
                # do not log tokens from registration messages
                _message["metadata"]["token"] = None
            message = json.dumps(_message)
        except:
            pass
        logger.debug(message)

    client.on('message', echo)
    client.once('open', load_skills_callback)
    client.run_forever()


if __name__ == "__main__":
    main()
