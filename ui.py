#!/usr/bin/env python3
"""
Simple UI for cue-vox - dot in center that changes with state
"""

import tkinter as tk
from enum import Enum


class State(Enum):
    """Voice interface states"""
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"


class VoxUI:
    """Minimal UI - just a dot that changes based on state"""

    STATE_COLORS = {
        State.IDLE: "#333333",           # Dark gray
        State.RECORDING: "#FF0000",      # Red
        State.TRANSCRIBING: "#FFA500",   # Orange
        State.THINKING: "#0080FF",       # Blue
        State.SPEAKING: "#00FF00",       # Green
    }

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("cue-vox")

        # Fullscreen, black background
        self.root.attributes('-fullscreen', True)
        self.root.configure(bg='black')

        # Exit on Escape
        self.root.bind('<Escape>', lambda e: self.root.quit())

        # Canvas for drawing dot
        self.canvas = tk.Canvas(
            self.root,
            bg='black',
            highlightthickness=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Dot size
        self.dot_radius = 50

        # Current state
        self.state = State.IDLE

        # Draw initial dot
        self.dot = None
        self.draw_dot()

    def draw_dot(self):
        """Draw or update the center dot"""
        # Get canvas center
        self.canvas.update()
        cx = self.canvas.winfo_width() // 2
        cy = self.canvas.winfo_height() // 2

        # Remove old dot
        if self.dot:
            self.canvas.delete(self.dot)

        # Draw new dot
        color = self.STATE_COLORS[self.state]
        self.dot = self.canvas.create_oval(
            cx - self.dot_radius,
            cy - self.dot_radius,
            cx + self.dot_radius,
            cy + self.dot_radius,
            fill=color,
            outline=""
        )

    def set_state(self, state: State):
        """Update state and redraw dot"""
        self.state = state
        self.draw_dot()

    def run(self):
        """Start UI event loop"""
        self.root.mainloop()


if __name__ == '__main__':
    ui = VoxUI()

    # Test state changes
    import threading
    import time

    def test_states():
        time.sleep(1)
        for state in [State.RECORDING, State.TRANSCRIBING,
                      State.THINKING, State.SPEAKING, State.IDLE]:
            ui.set_state(state)
            time.sleep(2)

    threading.Thread(target=test_states, daemon=True).start()
    ui.run()
