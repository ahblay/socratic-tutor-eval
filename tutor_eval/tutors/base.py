from abc import ABC, abstractmethod


class AbstractTutor(ABC):
    @abstractmethod
    def respond(self, student_message: str, history: list[dict]) -> str:
        """Return the tutor's next message.
        history: list of {"role": "student"|"tutor", "text": str}
        The student_message is already appended to history before this is called.
        """
        ...

    def session_state(self) -> dict | None:
        """Optionally expose internal session state (used for TBA metric).
        Returns None if not supported."""
        return None
