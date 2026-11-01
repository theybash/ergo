"""commandcenter: a private web console for a small fleet of Claude Code agents.

The design is in PRD.md and the interface rules are in DESIGN.md. Read those first; this
package is the implementation of decisions already made there.

Module map, in the order it is worth reading:

    config       paths, limits, and the tailnet check that is the front door
    registry     which agents exist, and nothing about whether they are running
    terminal     one pseudo-terminal per awake agent, streamed byte for byte
    broker       starting, waking and sleeping; owns every terminal, so liveness is free
    transcript   the session file on disk, read for cost and for the last message
    cords        transfer and call, the two ways work moves between agents
    health       machine telemetry, taken from healthd.py
    ws           WebSocket, server side, hand written on the standard library
    server       routing, the event stream, and the terminal sockets
"""

__version__ = "0.1.0"
