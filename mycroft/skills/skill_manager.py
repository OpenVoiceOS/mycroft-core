# Copyright 2017 Mycroft AI Inc.
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
"""Load, update and manage skills on this device."""
import os
from glob import glob
from threading import Thread, Event
from time import sleep, monotonic

from mycroft.enclosure.api import EnclosureAPI
from mycroft.configuration import Configuration
from mycroft.messagebus.message import Message
from mycroft.util.log import LOG
from mycroft.skills.settings import SkillSettingsDownloader
from mycroft.skills.skill_loader import SkillLoader

SKILL_MAIN_MODULE = '__init__.py'


def _shutdown_skill(instance):
    """Shutdown a skill.

    Call the default_shutdown method of the skill, will produce a warning if
    the shutdown process takes longer than 1 second.

    Arguments:
        instance (MycroftSkill): Skill instance to shutdown
    """
    try:
        ref_time = monotonic()
        # Perform the shutdown
        instance.default_shutdown()

        shutdown_time = monotonic() - ref_time
        if shutdown_time > 1:
            LOG.warning('{} shutdown took {} seconds'.format(instance.skill_id,
                                                             shutdown_time))
    except Exception:
        LOG.exception('Failed to shut down skill: '
                      '{}'.format(instance.skill_id))


class SkillManager(Thread):

    def __init__(self, bus, watchdog=None):
        """Constructor

        Arguments:
            bus (event emitter): Mycroft messagebus connection
            watchdog (callable): optional watchdog function
        """
        super(SkillManager, self).__init__()
        self.bus = bus
        # Set watchdog to argument or function returning None
        self._watchdog = watchdog or (lambda: None)
        self._stop_event = Event()
        self._connected_event = Event()
        self.config = Configuration.get()
        # TODO change this once XDG PR is submitted
        self.skills_directory = os.path.join(
            self.config["data_dir"], self.config["skills"]["msm"]["directory"])

        self.skill_loaders = {}
        self.enclosure = EnclosureAPI(bus)
        self.initial_load_complete = False
        self.num_install_retries = 0
        self.settings_downloader = SkillSettingsDownloader(self.bus)

        # Statuses
        self._alive_status = False  # True after priority skills has loaded
        self._loaded_status = False  # True after all skills has loaded

        self._define_message_bus_events()
        self.daemon = True

    def _define_message_bus_events(self):
        """Define message bus events with handlers defined in this class."""
        # Conversation management
        self.bus.on('skill.converse.request', self.handle_converse_request)

        # Update on initial connection
        self.bus.on(
            'mycroft.internet.connected',
            lambda x: self._connected_event.set()
        )

        # Update upon request
        self.bus.on('skillmanager.list', self.send_skill_list)
        self.bus.on('skillmanager.deactivate', self.deactivate_skill)
        self.bus.on('skillmanager.keep', self.deactivate_except)
        self.bus.on('skillmanager.activate', self.activate_skill)
        self.bus.on('mycroft.paired', self.handle_paired)
        self.bus.on(
            'mycroft.skills.settings.update',
            self.settings_downloader.download
        )

    @property
    def skills_config(self):
        return self.config['skills']

    def _start_settings_update(self):
        self.settings_downloader.download()
        LOG.info('Skill settings downloading has started')

    def handle_paired(self, _):
        """Trigger upload of skills manifest after pairing."""
        self._start_settings_update()

    def load_priority(self):
        priority_skills = self.skills_config.get("priority_skills", [])
        for skill_id in priority_skills:
            skill_dir = os.path.join(self.skills_directory, skill_id)
            if skill_dir not in self.skill_loaders:
                LOG.info(f"Loading priority skill: {skill_id}")
                self._load_skill(skill_dir)
        self._alive_status = True

    def run(self):
        """Load skills and update periodically from disk and internet."""
        self._connected_event.wait()
        self._load_on_startup()

        # Scan the file folder that contains Skills.  If a Skill is updated,
        # unload the existing version from memory and reload from the disk.
        while not self._stop_event.is_set():
            try:
                self._unload_removed_skills()
                self._reload_modified_skills()
                self._load_new_skills()
                self._watchdog()
                sleep(2)  # Pause briefly before beginning next scan
            except Exception:
                LOG.exception('Something really unexpected has occured '
                              'and the skill manager loop safety harness was '
                              'hit.')
                sleep(30)

    def _load_on_startup(self):
        """Handle initial skill load."""
        LOG.info('Loading installed skills...')
        self._load_new_skills()
        LOG.info("Skills all loaded!")
        self.bus.emit(Message('mycroft.skills.initialized'))
        self._loaded_status = True

    def _reload_modified_skills(self):
        """Handle reload of recently changed skill(s)"""
        for skill_dir in self._get_skill_directories():
            try:
                skill_loader = self.skill_loaders.get(skill_dir)
                if skill_loader is not None and skill_loader.reload_needed():
                    skill_loader.reload()
            except Exception:
                LOG.exception('Unhandled exception occured while '
                              'reloading {}'.format(skill_dir))

    def _load_new_skills(self):
        """Handle load of skills installed since startup."""
        for skill_dir in self._get_skill_directories():
            if skill_dir not in self.skill_loaders:
                self._load_skill(skill_dir)

    def _load_skill(self, skill_directory):
        skill_loader = SkillLoader(self.bus, skill_directory)
        try:
            load_status = skill_loader.load()
        except Exception:
            LOG.exception('Load of skill {} failed!'.format(skill_directory))
            load_status = False
        finally:
            self.skill_loaders[skill_directory] = skill_loader

        return skill_loader if load_status else None

    def _get_skill_directories(self):
        skill_glob = glob(os.path.join(self.skills_directory, '*/'))

        skill_directories = []
        for skill_dir in skill_glob:
            # TODO: all python packages must have __init__.py!  Better way?
            # check if folder is a skill (must have __init__.py)
            if SKILL_MAIN_MODULE in os.listdir(skill_dir):
                skill_directories.append(skill_dir.rstrip('/'))
            else:
                LOG.debug('Found skills directory with no skill: ' + skill_dir)

        return skill_directories

    def _unload_removed_skills(self):
        """Shutdown removed skills."""
        skill_dirs = self._get_skill_directories()
        # Find loaded skills that don't exist on disk
        removed_skills = [
            s for s in self.skill_loaders.keys() if s not in skill_dirs
        ]
        for skill_dir in removed_skills:
            skill = self.skill_loaders[skill_dir]
            LOG.info('removing {}'.format(skill.skill_id))
            try:
                skill.unload()
            except Exception:
                LOG.exception('Failed to shutdown skill ' + skill.id)
            del self.skill_loaders[skill_dir]

    def is_alive(self, message=None):
        """Respond to is_alive status request."""
        return self._alive_status

    def is_all_loaded(self, message=None):
        """ Respond to all_loaded status request."""
        return self._loaded_status

    def send_skill_list(self, _):
        """Send list of loaded skills."""
        try:
            message_data = {}
            for skill_dir, skill_loader in self.skill_loaders.items():
                message_data[skill_loader.skill_id] = dict(
                    active=skill_loader.active and skill_loader.loaded,
                    id=skill_loader.skill_id
                )
            self.bus.emit(Message('mycroft.skills.list', data=message_data))
        except Exception:
            LOG.exception('Failed to send skill list')

    def deactivate_skill(self, message):
        """Deactivate a skill."""
        try:
            for skill_loader in self.skill_loaders.values():
                if message.data['skill'] == skill_loader.skill_id:
                    skill_loader.deactivate()
        except Exception:
            LOG.exception('Failed to deactivate ' + message.data['skill'])

    def deactivate_except(self, message):
        """Deactivate all skills except the provided."""
        try:
            skill_to_keep = message.data['skill']
            LOG.info('Deactivating all skills except {}'.format(skill_to_keep))
            loaded_skill_file_names = [
                os.path.basename(skill_dir) for skill_dir in self.skill_loaders
            ]
            if skill_to_keep in loaded_skill_file_names:
                for skill in self.skill_loaders.values():
                    if skill.skill_id != skill_to_keep:
                        skill.deactivate()
            else:
                LOG.info('Couldn\'t find skill ' + message.data['skill'])
        except Exception:
            LOG.exception('An error occurred during skill deactivation!')

    def activate_skill(self, message):
        """Activate a deactivated skill."""
        try:
            for skill_loader in self.skill_loaders.values():
                if (message.data['skill'] in ('all', skill_loader.skill_id) and
                        not skill_loader.active):
                    skill_loader.activate()
        except Exception:
            LOG.exception('Couldn\'t activate skill')

    def stop(self):
        """Tell the manager to shutdown."""
        self._stop_event.set()
        self.settings_downloader.stop_downloading()

        # Do a clean shutdown of all skills
        for skill_loader in self.skill_loaders.values():
            if skill_loader.instance is not None:
                _shutdown_skill(skill_loader.instance)

    def handle_converse_request(self, message):
        """Check if the targeted skill id can handle conversation

        If supported, the conversation is invoked.
        """
        skill_id = message.data['skill_id']

        # loop trough skills list and call converse for skill with skill_id
        skill_found = False
        for skill_loader in self.skill_loaders.values():
            if skill_loader.skill_id == skill_id:
                skill_found = True
                if not skill_loader.loaded:
                    error_message = 'converse requested but skill not loaded'
                    self._emit_converse_error(message, skill_id, error_message)
                    break
                try:
                    utterances = message.data['utterances']
                    lang = message.data['lang']
                    result = skill_loader.instance.converse(utterances, lang)
                    self._emit_converse_response(result, message, skill_loader)
                except Exception:
                    error_message = 'exception in converse method'
                    LOG.exception(error_message)
                    self._emit_converse_error(message, skill_id, error_message)
                finally:
                    break

        if not skill_found:
            error_message = 'skill id does not exist'
            self._emit_converse_error(message, skill_id, error_message)

    def _emit_converse_error(self, message, skill_id, error_msg):
        """Emit a message reporting the error back to the intent service."""
        reply = message.reply('skill.converse.response',
                              data=dict(skill_id=skill_id, error=error_msg))
        self.bus.emit(reply)

    def _emit_converse_response(self, result, message, skill_loader):
        reply = message.reply(
            'skill.converse.response',
            data=dict(skill_id=skill_loader.skill_id, result=result)
        )
        self.bus.emit(reply)
