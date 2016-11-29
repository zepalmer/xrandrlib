#!/usr/bin/env python

"""
This module defines a class, Xrandr, which represents the current display state
of X according to the xrandr command-line tool.
"""

import logging
import re
import subprocess
import distutils.spawn

from .utils import LineBuffer

_REGEX_SCREEN_HEADER = re.compile(
    r'Screen (?P<num>[0-9]+): '
    r'minimum (?P<minW>[0-9]+) x (?P<minH>[0-9]+), '
    r'current (?P<curW>[0-9]+) x (?P<curH>[0-9]+), '
    r'maximum (?P<maxW>[0-9]+) x (?P<maxH>[0-9]+)'
    )
_REGEX_OUTPUT_HEADER = re.compile(
    r'(?P<name>[^\s]+) '
    r'(?P<status>disconnected|connected|unknown connection) '
    r'('
        r'(?P<width>[0-9]+)'
        r'x(?P<height>[0-9]+)'
        r'('
            r'\+(?P<xpos>[0-9]+)'
            r'\+(?P<ypos>[0-9]+)'
        r')?'
    r' )?'
    r'('
        r'\(0x(?P<mode_id>[0-9a-f]+)\)'
    r' )?'
    )
_REGEX_MODE_HEADER = re.compile(
    r'  (?P<width>[0-9]+)x(?P<height>[0-9]+) '
    r'\(0x(?P<mode_id>[0-9a-f]+)\) '
    r'[0-9]+\.[0-9]+[GMK]?Hz'
    r' ?(?P<flags>(([+-][HV]Sync|Interlace) ?)+)'
    r'(?P<current>\*current)? ?'
    r'(?P<preferred>\+preferred)?'
    )

class XrandrError(Exception):
    """
    An exception type raised when an error occurs in the Xrandr library.
    """
    pass

class XrandrCommandError(XrandrError):
    """
    An exception type raised when a subprocess invocation of the xrandr binary
    fails or does not behave as expected.
    """
    pass

class XrandrContextError(XrandrError):
    """
    An exception type raised when an Xrandr object is used after it has been
    invalidated.
    """
    pass

class XrandrUpdatePolicy:
    """
    Represents the different update policies used when an Xrandr object is
    changed.
    """
    IMMEDIATE = 101
    DEFERRED = 102
    every = [IMMEDIATE, DEFERRED]

class XrandrRelativePosition:
    """
    Represents the relative positions that can be used in positioning an output.
    """
    LEFT_OF = "--left-of"
    RIGHT_OF = "--right-of"
    ABOVE = "--above"
    BELOW = "--below"
    SAME_AS = "--same-as"
    every = [LEFT_OF, RIGHT_OF, ABOVE, BELOW, SAME_AS]

class Xrandr(object):
    """
    A class representing the current display state.
    """

    def __init__(self,
                 xrandr_binary=distutils.spawn.find_executable('xrandr'),
                 update_policy=XrandrUpdatePolicy.DEFERRED):
        """
        Constructs a new Xrandr context.  Most programs should only need one
        object of this type.
        """
        if update_policy not in XrandrUpdatePolicy.every:
            raise ValueError("Invalid update policy")
        self.screen = None
        self._generation_id = 0
        self._xrandr_binary = xrandr_binary
        self._update_policy = update_policy
        self._pending_updates = {}
        self.refresh()

    def _run_xrandr(self, args=[]):
        command = [self._xrandr_binary] + args
        process = subprocess.Popen(
            command, close_fds=True, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stdout, _ = process.communicate(input="")
        exitcode = process.wait()
        if exitcode != 0:
            raise XrandrCommandError(
                "Error running xrandr command \"{}\": {}".format(
                    " ".join(command), stdout))
        return LineBuffer(map(lambda s: s.rstrip(),stdout.decode().split('\n')))

    def refresh(self):
        """
        Completely refreshes this context (akin to reconstructing the object).
        Any model objects previously obtained from this Xrandr object become
        invalid.
        """
        self._generation_id += 1
        self._pending_updates = {}
        lines = self._run_xrandr(args=['--verbose'])
        # FIXME: Naively assuming that all xrandr calls will discuss exactly
        # one screen...
        self.screen = self._parse_screen(lines)

    def _perform_update(self, object_identifier, property_name, xrandr_args):
        self._pending_updates[(object_identifier, property_name)] = xrandr_args
        if self._update_policy == XrandrUpdatePolicy.IMMEDIATE:
            self.commit_updates()

    def commit_updates(self):
        """
        Applies any pending updates which have not yet been applied.
        """
        args = []
        for v in self._pending_updates.itervalues():
            args.extend(v)
        self._run_xrandr(args)
        self._pending_updates = {}
        self.refresh()

    def _parse_screen(self, lines):
        screen_line = lines.next()
        m = _REGEX_SCREEN_HEADER.match(screen_line)
        if not m:
            raise XrandrCommandError(
                "Could not parse Screen line: {}".format(screen_line))
        outputs = []
        while _REGEX_OUTPUT_HEADER.match(lines.peek()):
            outputs.append(self._parse_output(lines))
        while lines.has_next() and lines.peek().strip() == "":
            lines.next()
        if lines.has_next():
            raise XrandrCommandError(
                "Could not parse xrandr output line: {}".format(lines.peek()))
        return Screen(self, int(m.group("num")),
                      (int(m.group("minW")), int(m.group("minH"))),
                      (int(m.group("curW")), int(m.group("curH"))),
                      (int(m.group("maxW")), int(m.group("maxH"))),
                      outputs)

    def _parse_output(self, lines):
        output_header_line = lines.next()
        m = _REGEX_OUTPUT_HEADER.match(output_header_line)
        if not m:
            raise XrandrCommandError(
                "Could not parse Output header line: {}".format(
                    output_header_line))
        name = m.group("name")
        if m.group("status") == "connected":
            connected = True
        elif m.group("status") == "disconnected":
            connected = False
        else:
            connected = None
        if m.group("width") is not None:
            size = (int(m.group("width")), int(m.group("height")))
        else:
            size = None
        if m.group("xpos") is not None:
            position = (int(m.group("xpos")), int(m.group("ypos")))
        else:
            if size is None:
                position = None
            else:
                position = (0,0)
        if m.group("mode_id") is not None:
            current_mode_id = int(m.group("mode_id"), 16)
        else:
            current_mode_id = None
        # Read all of the following lines to extract mode data
        modes = []
        while lines.has_next(): # there's a break below
            # Skip anything that looks like supplementary data
            if lines.peek().startswith("\t"):
                lines.next()
            elif _REGEX_MODE_HEADER.match(lines.peek()):
                modes.append(self._parse_mode(name, lines))
            elif not lines.peek().startswith(" "):
                break
            else:
                raise XrandrCommandError(
                    ("Could not parse xrandr Output supplementary line: "
                     "{}").format(lines.peek()))
        # Produce resulting output object
        return Output(self, name, connected, size, position, current_mode_id,
                      modes)

    def _parse_mode(self, output_name, lines):
        mode_header_line = lines.next()
        m = _REGEX_MODE_HEADER.match(mode_header_line)
        if not m:
            raise XrandrCommandError(
                "Could not parse Mode header line: {}".format(
                    mode_header_line))
        # Discard next two lines of metadata
        lines.next()
        lines.next()
        # Build Mode object
        size = (int(m.group("width")), int(m.group("height")))
        id = int(m.group("mode_id"), 16)
        flags = (m.group("flags") or "").strip().split()
        print m.groupdict()
        preferred = m.group("preferred") is not None
        return Mode(self, output_name, size, id, preferred, flags)

    def __str__(self):
        return "Xrandr object\n{}".format(self.screen)

class XrandrModelObject(object):
    """
    A superclass for all Xrandr model objects.  This model object retains a
    reference to the Xrandr object that created it.  This back reference permits
    a form of centralization in the model, allowing the invalidation of model
    objects as well as other features such as batch operations.
    """

    def __init__(self, master, identifier):
        """
        Creates a new XrandrModelObject.  The Xrandr object creating this object
        must be provided as the master.  The provided identifier should be
        unique to the master object for a particular generation ID.
        """
        self._master = master
        self._generation_id = self._master._generation_id
        self._identifier = identifier

    def is_valid(self):
        """
        Determines whether this XrandrModelObject is still valid.
        """
        return self._generation_id == self._master._generation_id

    def _require_valid(self):
        if not self.is_valid():
            raise XrandrContextError("Use of invalidated %s object" %
                                     self.__class__.__name__)

class Screen(XrandrModelObject):
    """
    A class representing an RandR screen.  The Screen has the following
    attributes:
        number: The X screen ID.
        size_min: A tuple describing the screen's minimum size (W,H).
        size_current: A tuple describing the screen's current size (W,H).
        size_max: A tuple describing the screen's maximum size (W,H).
        outputs: A list of the outputs associated with this screen.
    """

    def __init__(self, master, number, size_min, size_current, size_max,
                 outputs):
        super(Screen, self).__init__(master, "Screen({})".format(number))
        self.number = number
        self.size_min = size_min
        self.size_current = size_current
        self.size_max = size_max
        self.outputs = outputs

    def __str__(self):
        buf = "Screen {}: minimum {} x {}, current {} x {}, "\
              "maximum {} x {}".format(
                  self.number,
                  self.size_min[0], self.size_min[1],
                  self.size_current[0], self.size_current[1],
                  self.size_max[0], self.size_max[1])
        for output in self.outputs:
            buf += "\n{}".format(output)
        return buf

class Output(XrandrModelObject):
    """
    A class representing an RandR output.  The Output has the following
    attributes:
        connected: A boolean indicating if the output is connected.  If this
                   status is unknown, None is used.
        size: A tuple describing the size of the output in pixels (W,H).
        position: A tuple describing the location of the output in pixel
                  coordinates (X,Y).
        current_mode_id: The ID of the current screen mode.
        modes: A list of available modes (in the form of Mode objects) for this
               output.
    """

    def __init__(self, master, name, connected, size, position, current_mode_id,
                 modes):
        super(Output, self).__init__(master, "Output({})".format(name))
        self.name = name
        self.connected = connected
        self.size = size
        self.position = position
        self.current_mode_id = current_mode_id
        self.modes = modes

    def __str__(self):
        buf = "Output {} is {}".format(
            self.name,
            "connected" if self.connected is True else \
            "disconnected" if self.connected is False else \
            "status unknown")
        if self.size:
            buf += " ({}x{}".format(self.size[0], self.size[1])
            if self.position:
                buf += "+{}+{}".format(self.position[0], self.position[1])
            buf += ", mode ID {})".format(hex(self.current_mode_id))
        for mode in self.modes:
            buf += "\n  {}".format(mode)
        return buf

    def get_preferred_mode(self):
        """
        Returns the preferred mode for this output (or None if no such mode
        exists).
        """
        for mode in self.modes:
            print mode
            if mode.preferred:
                return mode
        return None

    def set_mode(self, mode):
        self._master._perform_update(
            self._identifier, "mode",
            ["--output", self.name, "--mode", str(hex(mode.id))])

    def set_position(self, x, y):
        self._master._perform_update(
            self._identifier, "position",
            ["--output", self.name, "--pos", "{}x{}".format(x,y)])

    def set_position_relative_to(self, relative_pos, output):
        if relative_pos not in XrandrRelativePosition.every:
            raise ValueError("Invalid relative position")
        self._master._perform_update(
            self._identifier, "position",
            ["--output", self.name, relative_pos, output.name])

class Mode(XrandrModelObject):
    """
    A class representing a display mode on an RandR output.  The Mode has the
    following attributes:
        size: A tuple describing the size of the mode (W,H).
        id: The ID of this mode.
        preferred: Whether this is the preferred mode for the output.
        flags: A list of the flags (e.g. "+HSync") set for this mode.
        refresh_rate: The refresh rate for this mode (in hertz).
    """

    def __init__(self, master, output_name, size, id, preferred, flags):
        super(Mode, self).__init__(master,
                                   "Mode({},{})".format(output_name, hex(id)))
        self.size = size
        self.id = id
        self.preferred = preferred
        self.flags = flags

    def __str__(self):
        return "{}x{} ({}) {}{}".format(
            self.size[0], self.size[1], hex(self.id), " ".join(self.flags),
            " preferred" if self.preferred else "")
