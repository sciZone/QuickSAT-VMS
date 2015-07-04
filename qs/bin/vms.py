#!/usr/bin/env python

import traceback
import sys
import time
import json
import syslog
import importlib

import mct
import vms_db
import mcp_target
import periodic_timer

def write_json_data(data, filename):
    f = open(filename, 'w')
    f.write(json.dumps(data))
    f.close()

def cmd_find_apps(cmds, apps):
    found_apps = []
    for i in range(len(cmds)):
        app_id = str(cmds[i]['data'])
        try:
            # force the comparison to be a string comparison
            #app = next(itertools.ifilter(lambda x: str(x['id']) == app_id, apps))
            app = filter(lambda x: str(x['id']) == app_id, apps)[0]
        except StopIteration:
            raise Exception('app {} not found in {}'.format(app_id, str(apps)))
        cmds[i]['app'] = app
        found_apps.append(app)
    return (cmds, found_apps)

class vms(object):
    def __init__(self, mcp_address, mcp_port, mcp_username, mcp_password, vms_address, vms_port, vms_cert, vms_username, vms_password, vms_dbname, domu_ip_range, **kwargs):
        # Save the arguments
        self.args = {
            'vms': {
                'address': vms_address,
                'port': vms_port,
                'username': vms_username,
                'password': vms_password,
                'cert': vms_cert,
                'dbname': vms_dbname
            },
            'mcp': {
                'address': mcp_address,
                'port': mcp_port,
                'username': mcp_username,
                'password': mcp_password
            },
            'domu': {
                'ip_range': domu_ip_range
            }
        }

        # Connect to the QS/VMS DB
        self.db = vms_db.vms_db(**self.args['vms'])

        # Connect to the MCP target
        self.mcp = mcp_target.mcp_target(**self.args['mcp'])

        self.commands = None

        self.threads = []

        # Open the syslog
        syslog.openlog()
        syslog.syslog(syslog.LOG_NOTICE, 'Started')

        # TODO: Initialize the periodic processing threads
        #self.sys_msgs_thread = periodic_timer.PeriodicTimer(self.periodic_system_messages, self.db.retrieve_system_msgs_poll_rate())
        #self.cmd_log_thread = periodic_timer.PeriodicTimer(self.periodic_command_log, self.db.retrieve_cmd_log_poll_rate())
        #self.flight_data_thread = periodic_timer.PeriodicTimer(self.periodic_flight_data, self.db.retrieve_flight_data_poll_rate())
        #self.input_dir_thread = periodic_timer.PeriodicTimer(self.monitor_input_dir, self.db.retrieve_command_poll_rate())

        # For now, use the command poll rate to run the "process" function
        t = periodic_timer.PeriodicTimer(self.process, self.db.retrieve_command_poll_rate())
        self.threads.append(t)
        
        # Use a pre-defined radio status poll time for now (39 seconds)
        t = periodic_timer.PeriodicTimer(self.radio_status, 39)
        self.threads.append(t)

    def radio_status(self):
        # Have the VMS DB connection retrieve and update the radio status
        self.db.get_radio_status()

        # Keep the poll rate constant for now, it shouldn't change
        return 39        


    def __del__(self):
        syslog.syslog(syslog.LOG_NOTICE, 'Shutting down')
        syslog.closelog()

    def run(self):
        for t in self.threads:
            t.start()

        try:
            while True:
                #print(time.asctime())
                time.sleep(60.0)
        except KeyboardInterrupt:
            for t in self.threads:
                t.stop()
            raise

    def periodic_system_messages(self, _timestamp=[0]):
        (logs, _timestamp[0]) = self.db.retrieve_system_messages(session, _timestamp[0])
        write_json_logs(data, '/opt/qs/outputs/system_messages_{}'.format(_timestamp[0]))
        return self.db.retrieve_system_msgs_poll_rate()

    def periodic_command_log(self, _timestamp=[0]):
        (log, _timestamp[0]) = self.db.retrieve_command_logs(session, _timestamp[0])
        write_json_data(log, '/opt/qs/outputs/command_log_{}'.format(_timestamp[0]))
        return self.db.retrieve_cmd_log_poll_rate()

    def periodic_flight_data(self, _timestamp=[0]):
        (data, _timestamp[0]) = self.db.retrieve_flight_data(session, _timestamp[0])
        write_json_data(data, '/opt/qs/outputs/flight_{}'.format(_timestamp[0]))
        return self.db.retrieve_flight_data_poll_rate()

    def monitor_input_dir(self, _timestamp=[0]):
        # TODO: periodically check the input directory (or DB?) for new files
        pass
        return self.db.retrieve_command_poll_rate()

    def process(self):
        self.commands = self.db.all_pending_commands()
        while self.commands:
            # Most likely there should only be one set of these commands queued
            # up, but it is possible that some combinations may be pending at
            # the same time.  To make the logic simpler, just check for all
            # possibilities
            if 'ADD_VMAPP' in self.commands or 'REMOVE_VMAPP' in self.commands:
                self.update_mcp()
            elif 'UPLOAD_VMAPP_TO_GATEWAY' in self.commands:
                pass
            elif 'DELETE_VMAPP_FROM_GATEWAY' in self.commands:
                pass
            elif 'START' in self.commands:
                self.start_mcp()
            elif 'STOP' in self.commands:
                self.stop_mcp()
            elif 'RESTART' in self.commands:
                self.restart_mcp()
            elif 'RETRIEVE_COMMAND_LOGS' in self.commands:
                self.retrieve_command_logs()
            elif 'RETRIEVE_SYSTEM_MESSAGES' in self.commands:
                self.retrieve_system_messages()
            elif 'RETRIEVE_FLIGHT_DATA' in self.commands:
                self.retrieve_flight_data()
            elif 'CREATE_REC_SESSION' in self.commands:
                self.create_rec_session()
            elif 'CALL' in self.commands:
            		self.call()
            elif 'HANGUP' in self.commands:
            		self.hangup()
            else:
                self.handle_unknown_command()

        # return the new poll rate if it has changed
        return self.db.retrieve_command_poll_rate()

    def update_mcp(self):
        if 'ADD_VMAPP' in self.commands:
            add_cmds = self.commands.pop('ADD_VMAPP')
        else:
            add_cmds = []
        if 'REMOVE_VMAPP' in self.commands:
            rem_cmds = self.commands.pop('REMOVE_VMAPP')
        else:
            rem_cmds = []
        try:
            # Connect to the MCP Target
            self.mcp.connect()

            # Retrieve all apps from the DB
            apps = self.db.getapps()

            # Locate the apps referenced by the commands
            (rem_cmds, rem_apps) = cmd_find_apps(rem_cmds, apps)
            (add_cmds, add_apps) = cmd_find_apps(add_cmds, apps)
            syslog.syslog(syslog.LOG_DEBUG, 'Update MCP apps: remove = "{}", add = "{}"'.format(rem_cmds, add_cmds))

            # Only apps on the target (apps with a state of > 100) should be
            # used to create the MCT, but apps that are being added should also
            # be included, and apps that are being removed should be excluded.
            mctapps = [a for a in apps if (a['state'] >= 100 and a not in rem_apps) or a in add_apps]
            mct_app_files = [a['name'] for a in mctapps]
            syslog.syslog(syslog.LOG_DEBUG, 'Apps for MCT = "{}"'.format(mctapps))

            # Only remove applications if they are not used by other application
            files = [ c['app']['name'] for c in rem_cmds if c not in mct_app_files]
            self.mcp.remove_files(files)

            # Add identified files from the MCP target (including the new MCT)
            files = [ c['app']['name'] for c in add_cmds ]
            self.mcp.add_files(files)

            # Construct the new MCT
            newmct = mct.mct()
            newmct.addapps(mctapps, self.args['mcp']['address'], self.args['domu']['ip_range'])
            newmct.close()

            # Restart MCP
            self.mcp.reload(newmct.path())
            self.mcp.close()

            # Update the state of the applications added or removed:
            #   50: Stored on the ground station (not visible on the gateway,
            #       but is on the ground station).
            #   80: Stored on the Gateway
            #   100: On the host and operational.
            #   101-199: Operational, on the host, with added messages and
            #       states.  Open and TBD
            #   200: On the host, but NOT operational.  For example the VM is
            #       there, but the executable has stopped.  This might be
            #       necessary for different modes of operation.  Essentially
            #       the VM is ready and waiting, but the app is not in use.
            #   201-299: similar code to 200, but open and TBD
            #   300-399: Error codes; VM and application are on the host.
            #
            # Because the commands should be marked as complete as soon as the
            # state is changed, we must complete the add/remove commands one at
            # a time.
            for c in rem_cmds[:]:
                # An app gets a state of "80" when it is removed from the
                # target (only present on the gateway)
                status = 'GATEWAY Storage'
                msg = 'Success - VM/App "{}" removed from Host'.format(c['app']['part'])
                self.db.set_application_state(c['app'], 80, status, msg)
                # Remove the command from the list to ensure that if a future
                # command status update fails, this command is not marked as
                # FAILED
                rem_cmds.remove(c)
                self.db.complete_commands(c, True)
            for c in add_cmds[:]:
                # An app gets a state of 100+ when it is moved to the target
                # 190 means the VM is initializing
                # 195 means that the app has been placed on the host and the
                #   VM is yet to be started
                status = 'On Host - VM Configured'
                msg = 'Success - VM/App "{}" installed'.format(c['app']['part'])
                self.db.set_application_state(c['app'], 195, status, msg)
                # Remove the command from the list to ensure that if a future
                # command status update fails, this command is not marked as
                # FAILED
                add_cmds.remove(c)
                self.db.complete_commands(c, True)
        except:
            self.db.complete_commands(add_cmds + rem_cmds, False, traceback.format_exception(*sys.exc_info()))

    def start_mcp(self):
        # There will probably only be 1 MCP start command, but more won't hurt
        cmds = self.commands.pop('START')
        try:
            self.mcp.connect()
            self.mcp.start()
            self.mcp.close()
            self.db.complete_commands(cmds, True)
        except:
            self.db.complete_commands(cmds, False, traceback.format_exception(*sys.exc_info()))

    def stop_mcp(self):
        # There will probably only be 1 MCP stop command, but more won't hurt
        cmds = self.commands.pop('STOP')
        try:
            self.mcp.connect()
            self.mcp.stop()
            self.mcp.close()
            self.db.complete_commands(cmds, True)
        except:
            self.db.complete_commands(cmds, False, traceback.format_exception(*sys.exc_info()))

    def restart_mcp(self):
        # There will probably only be 1 MCP restart command, but more won't hurt
        cmds = self.commands.pop('RESTART')
        try:
            self.mcp.connect()
            self.mcp.restart()
            self.mcp.close()
            self.db.complete_commands(cmds, True)
        except:
            self.db.complete_commands(cmds, False, traceback.format_exception(*sys.exc_info()))

    def retrieve_command_logs(self):
        # There are 3 different ways the command logs can be retrieved:
        #   1. within a time range
        #   2. command logs for a specific session
        #   3. all command logs
        #
        # Check the 'data' element to determine what type of command this is.
        # Each retrieve command should be processed separately
        for c in self.commands.pop('RETRIEVE_COMMAND_LOGS'):
            try:
                session=None
                timestamp=None
                # Check if there is any data specified at all
                if len(c['data']):
                    # First try an integer conversion
                    try:
                        session = int(c['data'])
                    except ValueError:
                        # Last possibility is a timestamp, use the standard MySQL
                        # timestamp format
                        timestamp = time.strptime(c['data'], '%Y-%m-%d %H:%M:%S')
                (msgs, _) = self.db.retrieve_command_logs(session, timestamp)
                write_json_data(msgs, '/opt/qs/outputs/cmd{}_command_log_{}'.format(c['time'], c['data']))
                self.db.complete_commands(c, True)
            except:
                self.db.complete_commands(c, False, traceback.format_exception(*sys.exc_info()))

    def retrieve_system_messages(self):
        # There are 3 different ways the system messages can be retrieved:
        #   1. within a time range
        #   2. system messages for a specific session
        #   3. all system messages
        #
        # Check the 'data' element to determine what type of command this is.
        # Each retrieve command should be processed separately
        for c in self.commands.pop('RETRIEVE_SYSTEM_MESSAGES'):
            try:
                session=None
                timestamp=None
                # Check if there is any data specified at all
                if len(c['data']):
                    # First try an integer conversion
                    try:
                        session = int(c['data'])
                    except ValueError:
                        # Last possibility is a timestamp, use the standard MySQL
                        # timestamp format
                        timestamp = time.strptime(c['data'], '%Y-%m-%d %H:%M:%S')
                (logs, _) = self.db.retrieve_system_messages(session, timestamp)
                write_json_data(logs, '/opt/qs/outputs/cmd{}_system_messages_{}'.format(c['time'], c['data']))
                self.db.complete_commands(c, True)
            except:
                self.db.complete_commands(c, False, traceback.format_exception(*sys.exc_info()))

    def retrieve_flight_data(self):
        # There are 3 different ways the flight data can be retrieved:
        #   1. within a time range
        #   2. flight data for a specific session
        #   3. all flight data
        #
        # Check the 'data' element to determine what type of command this is.
        # Each retrieve command should be processed separately
        for c in self.commands.pop('RETRIEVE_FLIGHT_DATA'):
            try:
                session=None
                timestamp=None
                # Check if there is any data specified at all
                if len(c['data']):
                    # First try an integer conversion
                    try:
                        session = int(c['data'])
                    except ValueError:
                        # Last possibility is a timestamp, use the standard MySQL
                        # timestamp format
                        timestamp = time.strptime(c['data'], '%Y-%m-%d %H:%M:%S')
                (data, _) = self.db.retrieve_flight_data(session, timestamp)
                write_json_data(data, '/opt/qs/outputs/cmd{}_flight_{}'.format(c['time'], c['data']))
                self.db.complete_commands(c, True)
            except:
                self.db.complete_commands(c, False, traceback.format_exception(*sys.exc_info()))

    def create_rec_session(self):
        # 
        #   Creating a new recording session, by incrementing from the last number used
        #   
        # 
        for c in self.commands.pop('CREATE_REC_SESSION'):
            try:
                self.db.increment_session()
                self.db.complete_commands(c, True)
            except:
                self.db.complete_commands(c, False, traceback.format_exception(*sys.exc_info()))
                
    def handle_unknown_command(self):
        # If none of the other commands matched, we don't know how to handle
        # the remaining command(s), mark them as failed.
        for k in list(self.commands.keys()):
            # If the command is "STRING.STRING", split the string and attempt
            # to import a module to handle the command.
            cmd = k.split('.', 2)
            cmd_list = self.commands.pop(k)
            if len(cmd) == 2:
                try:
                    m = importlib.import_module(cmd[0])
                    cmd_process_func = getattr(m, 'process')
                except:
                    # The previous statements could fail if a custom command
                    # package is not defined, or if it does not have a process
                    # function defined.
                    self.db.complete_commands(cmd_list, False, traceback.format_exception(*sys.exc_info()))
                    cmd_process_func = None

                if cmd_process_func:
                    for c in cmd_list:
                        try:
                            result = cmd_process_func(self.db, cmd[1], c['data'])
                            self.db.complete_commands(c, result)
                        except:
                            self.db.complete_commands(c, False, traceback.format_exception(*sys.exc_info()))
            else:
                # This is not a custom command, just log it as an unknown error
                for c in self.commands.pop(k):
                    msg = 'Unknown command {}:{}'.format(k, c)
                    self.db.complete_commands(c, False, msg)

    
    def call(self):
        cmds = self.commands.pop('CALL')
        try:
        		self.db.call('777')
            self.db.complete_commands(cmds, True)
        except:
            self.db.complete_commands(cmds, False, traceback.format_exception(*sys.exc_info()))

    def hangup(self):
        cmds = self.commands.pop('HANGUP')
        try:
            self.db.hangup()
            self.db.complete_commands(cmds, True)
        except:
            self.db.complete_commands(cmds, False, traceback.format_exception(*sys.exc_info()))
   