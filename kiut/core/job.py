# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/core/job.py -- A conversion as a job: its checks and questions, then the work

A conversion asks questions before it writes anything (corrected copies out
of date, designators KiCad won't take, files it would replace, ...). The
command line asks them as [y/N] questions or answers them from its options
(--yes), a window shows a dialog. The order of the checks and what follows
from each answer is the same in both, so it is written once, in a job; the
questions go to a Questions object of the front end's own:

- Job.run() runs the job's steps (Job.steps) and returns the exit code:
  0 done, 1 refused, cancelled or failed. A step that ends the job raises
  Stop with the lines the front end shows (the command line prints them).
  A job's steps are methods of their own, so a front end that asks in an
  order of its own (a window) can run them one by one.
- Job.say(text, level) reports a line (the command line prints it; a
  window logs it, as information or a warning).
- Questions: each tool's questions, one method each; a front end answers
  them its way (on the command line: [y/N] questions, or --yes).
  console_ask is the command line's [Y/n] / [y/N] question.

Each tool's job and questions are its own (kiut.board.kiub.BoardJob, ...);
the questions each asks, their order and wording are unchanged by being a
job (KIUT plan, stage 5).
"""
from __future__ import annotations


class Stop(Exception):
    """Ends a job: its exit code (1: refused, cancelled or failed) and the
    lines to show, in order (the command line prints them). refusal: for a
    refusal, its text as a window shows it (no "Error:" before each line);
    None for a cancelled job."""

    def __init__(self, *lines: str, code: int = 1, refusal: str | None = None) -> None:
        super().__init__(*lines)
        self.lines = list(lines)
        self.code = code
        self.refusal = refusal


class Job:
    """A conversion's checks, questions and work (steps), asked through
    questions (the front end's)."""

    def __init__(self, questions) -> None:
        self.questions = questions

    def steps(self) -> None:
        raise NotImplementedError

    def run(self) -> int:
        """Run the steps; the exit code (0 done)."""
        try:
            self.steps()
        except Stop as stop:
            for line in stop.lines:
                self.say(line)
            return stop.code
        return 0

    def say(self, text: str = '', level: str = 'info') -> None:
        """Report a line (the command line prints it; a window logs it with
        level: 'info' or 'warn')."""
        print(text)


def console_ask(question: str, default: bool, read=input, say=print) -> bool:
    """A yes/no question on the console, [Y/n] or [y/N] after the default
    (an empty answer). Without an answer to read (input closed, e.g. a
    scheduled run) the default is taken, and said."""
    hint = '[Y/n]' if default else '[y/N]'
    try:
        answer = read(f"{question} {hint}: ").strip().lower()
    except EOFError:
        say(f"\n  No answer to read: {'yes' if default else 'no'} (the default).")
        return default
    return default if not answer else answer in ('y', 'yes')

