"""What a LinuxCNC pin does, for the "Still to connect" card.

The text is taken from LinuxCNC's own documentation (the 2.10 sources:
docs/src/man/man9/motion.9.adoc, man1/halui.1.adoc, config/core-components.adoc,
plasma/qtplasmac.adoc), shortened, with links to the pages on linuxcnc.org.
Nothing is fetched while the window runs: the text is here, the window opens
the links in the user's browser. Pins of the user's own components have no
entry (nothing documents them); neither do pins whose text could not be
checked in the sources.

When a pin is documented differently by a later LinuxCNC, edit PATTERNS.
"""

import re
from dataclasses import dataclass

BASE = "https://linuxcnc.org/docs/html/"

# key -> (title, page path, section anchor of the pins or "")
PAGES = {
    "qtplasmac": ("QtPlasmaC", "plasma/qtplasmac.html", "_available_i_os"),
    "plasmac": ("plasmac component", "man/man9/plasmac.9.html", ""),
    "motion": ("motion (motion, joint and spindle pins)", "man/man9/motion.9.html", ""),
    "iocontrol": ("iocontrol", "config/core-components.html", "_iocontrol"),
    "halui": ("halui", "man/man1/halui.1.html", ""),
}

# (pin regex, page key, text); the first match counts.
PATTERNS = [
    # iocontrol
    (r"iocontrol\.0\.emc-enable-in", "iocontrol",
     "The external E-stop. Drive it FALSE when an external E-stop condition exists. "
     "Usually it is wired through iocontrol.0.user-enable-out and the machine's E-stop chain."),
    (r"iocontrol\.0\.user-enable-out", "iocontrol",
     "FALSE when an internal E-stop condition exists. Often connected to the E-stop chain "
     "that ends in iocontrol.0.emc-enable-in."),
    (r"iocontrol\.0\.user-request-enable", "iocontrol",
     "TRUE when the user has asked for the E-stop to be cleared (the reset of an external "
     "E-stop circuit)."),
    (r"iocontrol\.0\.coolant-flood", "iocontrol", "TRUE when flood coolant is requested (M8)."),
    (r"iocontrol\.0\.coolant-mist", "iocontrol", "TRUE when mist coolant is requested (M7)."),
    # motion
    (r"motion\.digital-in-\d\d", "motion",
     "A general digital input that G-code reads with M66 P<n> (wait for input)."),
    (r"motion\.digital-out-\d\d", "motion",
     "A general digital output that G-code sets with M62 to M65."),
    (r"motion\.analog-in-\d\d", "motion",
     "A general analog input that G-code reads with M66 E<n> (wait for input)."),
    (r"motion\.analog-out-\d\d", "motion", "A general analog output that G-code sets with M67 and M68."),
    (r"motion\.probe-input", "motion",
     "The probe. G38.n uses it to see when the probe makes contact: TRUE for contact closed "
     "(touching), FALSE for open."),
    (r"motion\.feed-hold", "motion",
     "When Feed Stop Control is enabled with M53 P1 and this is TRUE, the feed rate is set "
     "to 0. It applies to G-code, not to jogs."),
    (r"motion\.enable", "motion",
     "If driven FALSE, motion stops, the machine goes to \"machine off\" and a message is shown. "
     "Drive it TRUE for normal motion."),
    # joint
    (r"joint\.\d+\.home-sw-in", "motion",
     "Drive TRUE when the home switch of this joint is closed."),
    (r"joint\.\d+\.pos-lim-sw-in", "motion",
     "Drive TRUE when the positive limit switch of this joint is tripped."),
    (r"joint\.\d+\.neg-lim-sw-in", "motion",
     "Drive TRUE when the negative limit switch of this joint is tripped."),
    (r"joint\.\d+\.amp-fault-in", "motion",
     "Drive TRUE if an external fault is detected with this joint's amplifier."),
    (r"joint\.\d+\.amp-enable-out", "motion",
     "TRUE when this joint's amplifier should be enabled."),
    (r"joint\.\d+\.motor-pos-cmd", "motion", "The commanded position of this joint."),
    (r"joint\.\d+\.motor-pos-fb", "motion", "The actual position of this joint."),
    # spindle
    (r"spindle\.\d+\.on", "motion", "TRUE when the spindle should rotate."),
    (r"spindle\.\d+\.forward", "motion", "TRUE when the spindle should rotate forward."),
    (r"spindle\.\d+\.reverse", "motion", "TRUE when the spindle should rotate backward."),
    (r"spindle\.\d+\.brake", "motion", "TRUE when the spindle brake should be applied."),
    (r"spindle\.\d+\.speed-out", "motion", "The desired spindle speed in rotations per minute."),
    (r"spindle\.\d+\.speed-out-abs", "motion",
     "The desired spindle speed in rotations per minute, always positive."),
    (r"spindle\.\d+\.speed-out-rps", "motion", "The desired spindle speed in rotations per second."),
    (r"spindle\.\d+\.speed-out-rps-abs", "motion",
     "The desired spindle speed in rotations per second, always positive."),
    (r"spindle\.\d+\.at-speed", "motion",
     "Drive TRUE when the spindle is at its speed. Motion waits for it before the first feed "
     "move after each spindle start or speed change, before spindle-synchronised moves, and "
     "(in CSS mode) at each rapid to feed transition."),
    (r"spindle\.\d+\.is-oriented", "motion",
     "Acknowledges spindle-orient: completes the orient cycle."),
    (r"spindle\.\d+\.orient-fault", "motion",
     "A fault code (an integer) during spindle-orient: non-zero aborts the orient cycle "
     "and motion reports it."),
    (r"spindle\.\d+\.index-enable", "motion",
     "A handshake pin (it is read and written by both sides). For spindle-synchronised moves "
     "(threading, G33, G95) it must be connected to the index-enable pin of the spindle "
     "encoder: with an encoder channel of the slave, `<slave>.<channel>.index-enable`."),
    (r"spindle\.\d+\.revs", "motion",
     "The spindle position in revolutions. For spindle-synchronised moves it must be connected "
     "to the position pin of the spindle encoder: with an encoder channel of the slave, "
     "`<slave>.<channel>.pos` (with pos-scale in counts per revolution)."),
    (r"joint\.\d+\.index-enable", "motion",
     "A handshake pin (it is read and written by both sides). Connect it to the index-enable "
     "pin of the joint's encoder to home to the index pulse."),
    (r"spindle\.\d+\.speed-in", "motion",
     "The actual spindle speed in revolutions per second; used by G96 (constant surface "
     "speed) and G95 (feed per revolution)."),
    # halui
    (r"halui\.machine\.on", "halui", "A pulse here sets the machine on."),
    (r"halui\.machine\.off", "halui", "A pulse here sets the machine off."),
    (r"halui\.machine\.is-on", "halui", "TRUE while the machine is on."),
    (r"halui\.estop\.activate", "halui", "Sets LinuxCNC's internal E-stop."),
    (r"halui\.estop\.reset", "halui", "Resets LinuxCNC's internal E-stop."),
    (r"halui\.estop\.is-activated", "halui", "TRUE while LinuxCNC's internal E-stop is set."),
    (r"halui\.program\.run", "halui", "A pulse here runs the program."),
    (r"halui\.program\.pause", "halui", "A pulse here pauses the program."),
    (r"halui\.program\.resume", "halui", "A pulse here resumes the program."),
    (r"halui\.program\.step", "halui", "A pulse here steps in the program."),
    (r"halui\.program\.stop", "halui", "A pulse here stops the program."),
    (r"halui\.program\.is-running", "halui", "TRUE while a program runs."),
    (r"halui\.program\.is-paused", "halui", "TRUE while a program is paused."),
    (r"halui\.program\.is-idle", "halui", "TRUE while no program runs."),
    (r"halui\.mist\.on", "halui", "A pulse here starts mist."),
    (r"halui\.mist\.off", "halui", "A pulse here stops mist."),
    (r"halui\.flood\.on", "halui", "A pulse here starts flood."),
    (r"halui\.flood\.off", "halui", "A pulse here stops flood."),
    # plasmac
    (r"plasmac\.arc-voltage-in", "qtplasmac",
     "Analog input, optional: the arc voltage, from the velocity output of an encoder-equipped "
     "breakout board. Used to correct the torch height during cutting."),
    (r"plasmac\.arc-ok-in", "qtplasmac",
     "Digital input, optional: the Arc OK (transfer) output of the plasma power source. Tells "
     "that the arc is established and the machine may move."),
    (r"plasmac\.float-switch", "qtplasmac",
     "Digital input, optional: a switch on the floating head. Probes the work piece to set Z "
     "zero; also treated as a breakaway switch when not probing."),
    (r"plasmac\.ohmic-probe", "qtplasmac",
     "Digital input, optional: the output of the ohmic probe. Probes electrically through the "
     "work piece and torch consumables to set Z zero."),
    (r"plasmac\.breakaway", "qtplasmac",
     "Digital input, optional: a torch breakaway switch that senses that the torch has broken "
     "away from its cradle. The float switch is treated the same when not probing."),
    (r"plasmac\.move-up", "qtplasmac",
     "Digital input, optional (mode 2): the up output of an external THC, to move Z up and "
     "keep the torch height during cutting."),
    (r"plasmac\.move-down", "qtplasmac",
     "Digital input, optional (mode 2): the down output of an external THC, to move Z down "
     "and keep the torch height during cutting."),
    (r"plasmac\.torch-on", "qtplasmac",
     "Digital output, required: the torch-on input of the plasma power supply. Starts the arc."),
    (r"plasmac\.ohmic-enable", "qtplasmac",
     "Digital output, optional: controls the power of the ohmic probe."),
    (r"plasmac\.scribe-arm", "qtplasmac",
     "Digital output, optional: the scribe arming circuit; places the scribe on the work piece."),
    (r"plasmac\.scribe-on", "qtplasmac",
     "Digital output, optional: the scribe-on circuit; turns the scribing device on."),
]

_COMPILED = [(re.compile(p), k, t) for p, k, t in PATTERNS]


@dataclass
class PinInfo:
    text: str           # what the pin does
    title: str          # the page's name
    page_url: str       # the whole page
    pin_url: str        # the section about the pin (the page's, when it has no anchor)


def info(pin):
    """PinInfo of a LinuxCNC pin, or None (your own component, or nothing checked)."""
    for rx, key, text in _COMPILED:
        if rx.fullmatch(pin):
            title, path, anchor = PAGES[key]
            url = BASE + path
            return PinInfo(text, title, url, url + ("#" + anchor if anchor else ""))
    return None
