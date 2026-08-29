from ...domain.events.system import AlertFired

class OnSystemAlertHandler:
    def __init__(self):
        pass
        
    def handle(self, event: AlertFired) -> None:
        """Push to SSE/Notification Service"""
        print(f"[{event.level}] {event.message} - {event.details}")
