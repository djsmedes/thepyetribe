'''
A Python wrapper that implements the Eye Tribe API.
Copyright (C) 2014  Daniel Smedema

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

import threading
import json
import socket
from datetime import datetime
from copy import deepcopy
from time import sleep
from Queue import Queue, LifoQueue


class HeartThread(threading.Thread):
    
    '''Sends "heartbeats," i.e. keepalives, to the eye tribe server.'''
    
    def __init__(self, beat_function, interval=0.250):
        '''Initialize the object.
        
        Arguments:
        beat_function -- a function of no arguments that sends the
            required heartbeat message to the EyeTribeServer. 
            This thread is SUPPOSED to be incapable of working on its
            own without a proper EyeTribeServer object. We don't want
            to directly give it access to the socket or threading lock
            for compartmentalization reasons.
        interval -- the interval in between sending heartbeats, in
            seconds.
            Default is 0.250.
        '''
        super(HeartThread, self).__init__()
        self.beat = beat_function
        self.interval = interval
        self._stop = threading.Event()
        
    def stop(self):
        '''Set the stop flag.'''
        self._stop.set()
        
    def run(self):
        '''Keep on beating, every self.interval.'''
        while not self._stop.is_set():
            self.beat()
            sleep(self.interval)
                

class ListenerThread(threading.Thread):
    
    '''Continually listens for messages from the eye tribe server,
    and puts these into a Queue to be processed.
    '''
    
    def __init__(self, recv_function, q):
        '''Initialize the object.
        
        Arguments:
        recv_function -- a function of no arguments that receives data
            from the socket, i.e. lambda: socket.recv(BUFSIZE). 
            This thread is SUPPOSED to be incapable of working on its
            own without a proper EyeTribeServer object. We don't want
            to directly give it access to the socket or threading lock
            for compartmentalization reasons.
        q -- a Queue to put stuff in.
        '''
        super(ListenerThread, self).__init__()
        self.recv_function = recv_function
        self.q = q
        self._stop = threading.Event()
            
        
    def stop(self):
        '''Set the stop flag.'''
        self._stop.set()
        
    def run(self):
        '''Put anything from the socket into the Queue, separating on
        newlines.
        
        Returns:
        Nothing
        
        Side Effects:
        Fills the Queue with strings received from the tracker.
        '''
        while not self._stop.is_set():
            recv_str = self.recv_function()
            recv_strs = recv_str.split('\n')
            for str_ in recv_strs:
                if str_ == '':
                    continue
                else:
                    self.q.put(str_)
                    
                    
class ProcessorThread(threading.Thread):
    
    '''Takes the stuff from the Listener Thread's Queue and figures
    out what to do with it.
    '''
    
    def __init__(self,
                 raw_data_stream,
                 set_current_frame,
                 calibration_q,
                 tracker_q,
                 update_states,
                 frame_file=None):
        '''Initialize the object.
        
        Arguments:
        raw_data_stream -- the Queue that ListenerThread put stuff in
        set_current_frame -- should be a function of one argument that
            sets the _current_frame attribute of the calling EyeTribeServer
            class.
        calibration_q -- an EyeTribeQueue where we will put messages
            relating to calibration.
        tracker_q -- an EyeTribeQueue where we will put messages relating
            to attributes of the tracker. These will be mostly get and set
            commands.
        updates_states -- should be a a function of one argument that does
            something appropriate when called with a dict that was parsed
            from a json string in which the tracker is informing us about
            a change in one of its state variables: trackerstate, display,
            or calibration.
        frame_file -- an open file object where you want frame data to be
            recorded. Must be open already, as this function will be
            running continuously and opening and closing the file 60 times
            a second is a bad idea.
            Default value is None. If set to None, frames will not be
            recorded anywhere and only the most recent one will be kept.
        '''
        super(ProcessorThread, self).__init__()
        
        self._frame_file = frame_file
        self.raw_q = raw_data_stream
        self.set_current_frame = set_current_frame
        self.calibration_q = calibration_q
        self.tracker_q = tracker_q
        self.update_states = update_states
        
        self._frame_file_lock = threading.Lock()
        self._stop = threading.Event()
        
    @property
    def frame_file(self):
        return self._frame_file
    # The frame file can be changed. It will wait until it can acquire
    # the lock, meaning that it will not interrupt a write operation.
    # Then it will flush the old file's buffer, if there is one.
    @frame_file.setter
    def frame_file(self, file_):
        if file_ is not self._frame_file:
            with self._frame_file_lock:
                if self._frame_file is not None:
                    self._frame_file.flush()
                self._frame_file = file_
            
    def run(self):
        '''Process stuff out of the raw_q as fast as you can into the
        categorized EyeTribeQueues, and also write frames to a file if
        applicable.
        
        Returns:
        Nothing
        
        Side Effects:
        Removes things from the raw Queue.
        Fills the calibration Queue with calibration messages.
        Fills the tracker Queue with tracker messages.
        Writes frame data to a file, if one is given.
        Updates tracker state, calibration state, and display index.
        '''
        while not self._stop.is_set():
            # The tracker's firmware is probably written in C, because
            # -1.#IND is what you get in C from an invalid math op like
            # sqrt(-1). Python's json module does not parse that
            # properly, though, so we simply replace it with 'null' so
            # that json.loads will work.
            raw_msg = self.raw_q.get().replace('-1.#IND', 'null')
            msg = json.loads(raw_msg)
            
            if msg[u'statuscode'] in [800, 801, 802]:
                threading.Thread(
                        target=self.update_states, args=(msg,)
                        ).start()
                continue
            
            # Note that Python evaluates Boolean conditions lazily, so
            # you cannot get a KeyError on the second condition.
            if u'values' in msg.keys() and u'frame' in msg[u'values'].keys():
                self.set_current_frame(msg[u'values'][u'frame'])
                with self._frame_file_lock:
                # Thread safety; we want to make sure we don't try
                # to change the frame file in the middle of a write.
                    if self._frame_file is not None:
                        self._frame_file.write('{}\n'.format(raw_msg))
                continue
        
            if msg[u'category'] == u'tracker':
                self.tracker_q.put(msg)
            elif msg[u'category'] == u'calibration':
                self.calibration_q.put(msg)
            elif msg[u'category'] == u'heartbeat':
                # Currently we just ignore heartbeat replies, but they
                # could be used for error checking or something else.
                pass
            else:
                # The above conditions should exhaust the possibilities
                # but perhaps an error message here would be useful.
                pass

    def stop(self):
        '''Set the stop flag.'''
        self._stop.set()



class EyeTribeServer(object):

    '''Contains all the methods and properties necessary to make use
    of the eye tribe tracker with Python.
    '''
    
    def __init__(self, HOST="localhost", port=6555, BUFSIZE=4096):
        '''Initialize the class.
        
        Arguments:
        HOST -- the IP of the machine your tracker is connected to.
            Default is localhost. Note that if you want to connect to
            an Eye Tribe tracker on a remote computer, it is quite
            possible, but EyeTribeServer.exe must be running on that
            machine and configured to allow remote connections, and you
            must be able to get through firewalls to actually reach the
            machine.
        port -- the TCP port you will be using to talk to the EyeTribeServer.
            Default is 6555, also the default in eyetribe.cfg. You must
            change it there in order to use a different port.
        BUFSIZE -- the number of bytes the socket will recv at once.
            Default is 4096 based on recommendation from socket module.
        '''
        self.socket = socket.create_connection((HOST,port), None)
        self.lock = threading.Lock()
        self.raw_q = Queue()
        self._current_frame = None
        self.calibration_q = EyeTribeQueue()
        self.tracker_q = EyeTribeQueue()
        self._in_push_mode = False
        
        # Any code that could be affected by changes in the following
        # states should make use of these conditions!
        self.calibration_state_changed = threading.Condition()
        self.display_index_changed = threading.Condition()
        self.tracker_state_changed = threading.Condition()
        
        # Make and start the heartbeat thread.
        self.heart_thr = HeartThread(
                beat_function=lambda: self._send_message(u'heartbeat')
                )
        self.heart_thr.start()
        
        # Make and start the processor thread.
        self.processor_thr = ProcessorThread(
                raw_data_stream=self.raw_q,
                set_current_frame=self._set_current_frame,
                calibration_q=self.calibration_q,
                tracker_q=self.tracker_q,
                update_states=self._update_states
                )
        self.processor_thr.start()
        
        # Make and start the listener thread.
        self.listener_thr = ListenerThread(
                recv_function=lambda: self.socket.recv(BUFSIZE),
                q=self.raw_q
                )
        self.listener_thr.start()
        
    def _set_current_frame(self, frame_):
        '''Exists to be passed to ProcessorThread'''
        self._current_frame = frame_
    
    def _update_states(self, msg_dict):
        '''Notifies all functions waiting on a state change in the
        respective attribute of the tracker.
        
        Arguments:
        msg_dict -- a dict parsed from a json string that the tracker
            has sent.
            
        Returns:
        Nothing.
        
        Side effects:
        Anything waiting on the condition that gets changed will be
            notified.
        '''        
        if msg_dict[u'statuscode'] == 800:
            with self.calibration_state_changed:
                self.calibration_state_changed.notify_all()
        elif msg_dict[u'statuscode'] == 801:
            with self.display_index_changed:
                self.display_index_changed.notify_all()
        elif msg_dict[u'statuscode'] == 802:
            with self.tracker_state_changed:
                self.tracker_state_changed.notify_all()
        else:
            # error
            print msg_dict
        
    def _send_message(self, category, request=None, values=None):
        '''Send a message to the EyeTribeServer.
        Note that this probably should not be used by anything outside
        this module. When it is used within the module it only receives
        good input.
        
        Arguments:
        category -- should be a string or unicode that is either
            heartbeat, calibration, or tracker.
        request -- should be a string or unicode that is a valid
            request for the given category.
            Default is None; if left as None, it will be excluded from
            the message.
        values -- should be a dict or list containing appropriate
            values for the given category and request.
            Default is None; if left as None, it will be excluded from
            the message.
            
        Returns:
        Nothing.
        
        Notes:
        Replies will be received by the ListenerThread and sorted into
            the proper EyeTribeQueues by the ProcessorThread. From
            there, whatever slightly higher-level function called this
            one must fish out the appropriate reply using the
            EyeTribeQueue.get_item method.
        '''
        to_send = {}
        to_send[u'category'] = category
        if request is not None: 
            to_send[u'request'] = request
        if values is not None:
            to_send[u'values'] = values
        to_send = json.dumps(to_send)
        with self.lock:
            self.socket.send(to_send)
            
    def _send_calib_msg(self, request, values=None):
        '''Sends a message with category calibration.
        
        Arguments:
        request -- should be a string or unicode that is a valid
            request for calibration category.
        values -- should be a dict or list containing appropriate
            values for the given request.
            Default is None; if left as None, it will be excluded from
            the message.
            
        Returns:
        An item "get_item"ed from the calibration Queue that has a
            request identical to that of the sent message.
        '''
        self._send_message(u'calibration', request, values)
        return self.calibration_q.get_item(request, values)
    
    def _send_tracker_msg(self, request, values=None):
        '''Sends a message with category tracker.
        
        Arguments:
        request -- should be a string or unicode that is a valid
            request for tracker category.
        values -- should be a dict or list containing appropriate
            values for the given request.
            Default is None; if left as None, it will be excluded from
            the message.
            
        Returns:
        A item "get_item"ed from the tracker Queue that has a request
            identical to that of the sent message, and if the request
            was "get," identical values.
        '''
        self._send_message(u'tracker', request, values)
        return self.tracker_q.get_item(request, values)
    
    def _get_value(self, value_):
        '''Gets the value of value_.
        
        Arguments:
        value_ -- should be a gettable attribute of the eye tracker.
        
        Returns:
        The "got" value.
        
        '''
        reply = self._send_tracker_msg(u'get', [value_])
        return reply[u'values'][value_]
    
    def _set_value(self, key_, value_):
        '''Sets the value of key_ to value_.
        
        Arguments:
        key_ -- should be a mutable attribute of the eye tracker.
        value_ -- should be an appropriate value for the given key_.
        
        Returns:
        The reply to the set request. If the statuscode does not equal
            200, it means there was a problem and the value was
            probably not set, so that could be used to error check.
        '''
        return self._send_tracker_msg(u'set', {key_: value_})
    
    def _get_values(self, *args):
        '''Gets multiple values.
        
        Arguments:
        Any additional arguments passed into *args should be gettable
            attributes of the eye tracker.
        
        Returns:
        A list with the same length as *args, in which the elements are
            in the same order as they are in *args.
        '''
        reply = self._send_tracker_msg(u'get', args)
        to_return = [None]*len(args)
        for index, arg in enumerate(args):
            to_return[index] = reply[u'values'][arg]
        return to_return
    
    def _set_values(self, **kwargs):
        '''Sets multiple values.
        
        Arguments:
        Any additional keyword arguments passed to **kwargs should be
            settable attributes of the eye tracker.
            
        Returns:
        The reply to the set request. If the statuscode does not equal
            200, it means there was a problem and the value was
            probably not set, so that could be used to error check.
        '''
        return self._send_tracker_msg(u'set', kwargs)
    
    @property
    def push(self):
        return self._in_push_mode
    @push.setter
    def push(self, bool_):
        # We save this information so that we can check for it in the
        # getter for frame.
        self._in_push_mode = bool_
        return self._set_value(u'push', bool_)
    
    @property
    def heartbeatinterval(self):
        return self._get_value(u'heartbeatinterval')
    @heartbeatinterval.setter
    def heartbeatinterval(self, value_):
        raise EyeTribeError(2001)
    
    @property
    def version(self):
        return self._get_value(u'version')
    @version.setter
    def version(self, int_):
        return self._set_value(u'version', int_)
    
    @property
    def trackerstate(self):
        return self._get_value(u'trackerstate')
    @trackerstate.setter
    def trackerstate(self, value_):
        raise EyeTribeError(2001)
    
    @property
    def framerate(self):
        return self._get_value(u'framerate')
    @framerate.setter
    def framerate(self, value_):
        raise EyeTribeError(2001)
    
    @property
    def iscalibrated(self):
        return self._get_value(u'iscalibrated')
    @iscalibrated.setter
    def iscalibrated(self, value_):
        raise EyeTribeError(2001)
    
    @property
    def iscalibrating(self):
        return self._get_value(u'iscalibrating')
    @iscalibrating.setter
    def iscalibrating(self, value_):
        raise EyeTribeError(2001)
    
    @property
    def calibresult(self):
        return self._get_value(u'calibresult')
    @calibresult.setter
    def calibresult(self, value_):
        raise EyeTribeError(2001)
    
    @property
    def frame(self):
        # If we are not in push mode, we don't trust the _current_frame
        # to actually be current, so we wipe it and query the tracker
        # again. Then we wait until the ProcessorThread has updated it.
        # Probably a good idea to use threading.Condition()s here,
        # instead of that somewhat dangerous while-continue loop.
        if not self._in_push_mode:
            self._current_frame = None
            self._send_message(u'tracker', u'get', [u'frame'])
            while self._current_frame is None:
                continue
        return self._current_frame
    @frame.setter
    def frame(self, value_):
        raise EyeTribeError(2001)
    
    @property
    def screenindex(self):
        return self._get_value(u'screenindex')
    @screenindex.setter
    def screenindex(self, int_):
        return self._set_value(u'screenindex', int_)
    
    @property
    def screenresw(self):
        return self._get_value(u'screenresw')
    @screenresw.setter
    def screenresw(self, int_):
        return self._set_value(u'screenresw', int_)
    
    @property
    def screenresh(self):
        return self._get_value(u'screenresh')
    @screenresh.setter
    def screenresh(self, int_):
        return self._set_value(u'screenresh', int_)
    
    @property
    def screenpsyw(self):
        return self._get_value(u'screenpsyw')
    @screenpsyw.setter
    def screenpsyw(self, float_):
        return self._set_value(u'screenpsyw', float_)
    
    @property
    def screenpsyh(self):
        return self._get_value(u'screenpsyh')
    @screenpsyh.setter
    def screenpsyh(self, float_):
        return self._set_value(u'screenpsyh', float_)
    
    @property
    def screenres(self):
        return self._get_values(u'screenresw', u'screenresh')
    @screenres.setter
    def screenres(self, arg1, arg2=None):
        '''Accepts lists, tuples, or two distinct arguments.'''
        if type(arg1) == list or type(arg1) == tuple:
            if len(arg1) == 2:
                return self._set_values(
                        screenresw=arg1[0], screenresh=arg1[1]
                        )
            else:
                # error
                pass
        elif arg2 is not None:
            return self._set_values(screenresw=arg1, screenresh=arg2)
    
    def get_avg_xy(self):
        '''Get the smoothed x,y gaze coordinates.
        
        Returns:
        An x,y tuple.
        '''
        frame_ = self.frame
        return (frame_[u'avg'][u'x'], frame_[u'avg'][u'y'])
    
    def get_pupil_locations(self):
        '''Get the pupil locations.
        
        Returns:
        A tuple of tuples,
            ((left_eye_x, left_eye_y),(rite_eye_x,rite_eye_y))
        '''
        frame_ = self.frame
        lefteye = (frame_[u'lefteye'][u'pcenter'][u'x'],
                   frame_[u'lefteye'][u'pcenter'][u'y'])
        riteeye = (frame_[u'righteye'][u'pcenter'][u'x'],
                   frame_[u'righteye'][u'pcenter'][u'y'])
        return (lefteye, riteeye)
    
    def clear_calibration(self):
        '''Clear the current calibration.'''
        return self._send_calib_msg(u'clear')
        
    def start_calibration(self, num_calib_points):
        '''Start a new calibration.
        
        Arguments:
        num_calib_points -- an int between 7 and 16
        
        Returns:
        The EyeTribeServer's acknowledgement message as a dict.
        '''
        return self._send_calib_msg(
                u'start', {u'pointcount': num_calib_points}
                )
            
    def start_calib_point(self, x, y):
        '''Start a new calibration point.
        
        Arguments:
        x -- the x-coordinate of the point to be started
        y -- the y-coordinate of the point to be started
        
        Returns:
        The EyeTribeServer's acknowledgement message as a dict. 
        '''
        return self._send_calib_msg(u'pointstart', {u'x': x, u'y': y})
            
    def end_calib_point(self):
        '''End the current calibration point.
        
        Returns:
        The EyeTribeServer's response. If this is the last calibration
            point, this will contain the calibration data.
            But you can also get that data at any time from the
            calibresult property.
        '''
        # Maybe create a calibration attribute, and if the reply
        # contains certain things marking it as the calibration
        # data, then we store it in the calibration attribute?
        return self._send_calib_msg(u'pointend')
    
    def abort_calibration(self):
        '''Abort the current calibration.
        
        Returns:
        The EyeTribeServer's response.
        '''
        return self._send_calib_msg(u'abort')
    
    def record_data_to(self, file_):
        '''Sets the ProcessorThread's file that it will record to.
        
        Arguments:
        file_ -- must be an OPEN file object set to 'w' or 'a'
            or None to not record the data anywhere.
        
        Returns:
        Nothing
        '''
        self.processor_thr.frame_file = file_
    
    def est_cpu_minus_tracker_time(
            self,
            num_samples=200,
            cpu_diff_tolerance=0.001,
            sample_interval=0.016
            ):
        '''Return the difference between the internal clocks of the
        computer and the tracker.
        
        Arguments:
        num_samples -- how many samples will be taken and averaged.
            Default is 200.
        cpu_diff_tolerance -- the size of the time difference between
            cpu calls that will be tolerated. The first call is just
            before the frame is requested, and the second is just after
            it is successfully appended.
            Default is 0.001 s (1ms)
        sample_interval -- the number of seconds between samples.
            Default is 0.16, which will make it poll the tracker for
            frames just slightly faster than it can produce them. This
            means that every so often we will get a duplicate frame.
            However, we account for this, while it would be more
            difficult to account for skipping a frame.
            
        Returns:
        The mean difference between cpu timestamp and tracker timestamp
            in seconds.
            
        Errors:
        EyeTribeError with code 1001 if we attempt to use this function
            while in push mode.
        '''
        if self.push:
            raise EyeTribeError(1001)
        before_times = []
        tracker_times = []
        after_times = []
        # We save all the processing for later because we want
        # minimal additional computation between the cpu calls.
        # We actually get one more frame than the specified number to
        # simplify checking for duplicates.
        for _ in range(num_samples+1):
            before_times.append(datetime.now())
            tracker_times.append(self.frame[u'timestamp']) 
            after_times.append(datetime.now())
            sleep(sample_interval)
        avg_cpu_minus_tracker_time = 0
        num_frames_used = 0
        for i in range(num_samples):
            cpu_td = after_times[i]-before_times[i]
            # The below comparison would hit an IndexError if we did
            # not take one extra frame.
            if (tracker_times[i] == tracker_times[i+1]
                or cpu_td.total_seconds() > cpu_diff_tolerance):
                # If it is a duplicate or cpu calls are too far apart, skip
                continue
            tracker = datetime.strptime(
                    tracker_times[i], '%Y-%m-%d %H:%M:%S.%f'
                    )
            cpu_tracker_td = before_times[i] - tracker
            avg_cpu_minus_tracker_time += cpu_tracker_td.total_seconds()
            num_frames_used += 1
        avg_cpu_minus_tracker_time /= num_frames_used
        return avg_cpu_minus_tracker_time

    
class EyeTribeError(Exception):
    
    '''An error related to this module.'''
    
    err_msg_dict = {
            1001: 'Cannot estimate cpu-tracker time difference while '\
                  'in push mode.',
            2001: 'You cannot set this value, it is determined by the '\
                  'EyeTribeServer.',
            3001: 'You cannot use the get method in an EyeTribeQueue. Use'\
                  ' the get_item method instead.'
            }
    
    def __init__(self, error=None):
        self.err_msg = (error, EyeTribeError.err_msg_dict[error])
                
    def __str__(self):
        return self.err_msg 
    
    
class EyeTribeQueue(LifoQueue):
     
    '''A special Queue that only allows get_item() and not get(),
    so that you can get an item with a specific attribute from the
    Queue.
     
    Inherits from LifoQueue because that uses a basic list as its
    underlying data structure, enabling easy search. The reason we
    inherit from a Queue at all is because this needs to be
    thread-safe, and Queues already have that machinery in
    place.
    '''    
     
    def get(self, block=True, timeout=None):
        # fail
        raise EyeTribeError(3001)
             
    def get_item(self, request, values=None):
        '''Get an item from the Queue that matches the given request,
        and if applicable, values.
        
        Arguments:
        request -- should be a valid request for the particular Queue
            you are getting from. For the tracker Queue, this would be
            get or set; for the calibration Queue start, pointstart,
            pointend, abort, or clear. This may not be a complete list
            of valid requests.
        values -- should be a list of the attributes you have
            previously queried the tracker for. Only relevant if the
            request is get, because we assume that for other requests
            you will not be able to make them fast enough to ever get
            the wrong one.
            Default is None.
        
        Returns:
        An dict that was created by parsing a json string sent by the
            tracker.
        '''
        self.not_empty.acquire()
        try:
            while True: 
                for i in range(self._qsize()):
                    if self.queue[i][u'request'] == request:
                        if (request == u'get'
                            and set(self.queue[i][u'values']) != set(values)):
                            # If the request is 'get', we need to make
                            # sure we are getting the reply that has
                            # the values that were actually requested.
                            # Therefore if the item we are currently
                            # looking at has a different set of values,
                            # even though the request was the correct
                            # one, we don't want to return that item. 
                            continue
                        item = deepcopy(self.queue[i])
                        del self.queue[i]
                        self.not_full.notify()
                        return item
                self.not_empty.wait()
        finally:
            self.not_empty.release()
            
