"""
Client Manager — wraps wizwalker.ClientHandler with a clean interface
for detecting, connecting, and disconnecting Wizard101 game clients.
"""

from typing import List, Optional, Callable

from wizwalker.client_handler import ClientHandler
from wizwalker.client import Client


class ClientManager:
    """Manages Wizard101 client detection and hook lifecycle."""

    def __init__(self):
        self._handler = ClientHandler()
        self._active_client: Optional[Client] = None
        self._connected = False
        self._on_status_change: Optional[Callable[[str], None]] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active_client(self) -> Optional[Client]:
        """The currently connected client, or None."""
        return self._active_client if self._connected else None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def clients(self) -> List[Client]:
        """All currently tracked clients (connected or not)."""
        return list(self._handler.clients)

    # ------------------------------------------------------------------
    # Status callback
    # ------------------------------------------------------------------

    def set_status_callback(self, callback: Callable[[str], None]):
        """Register a callback that receives status messages."""
        self._on_status_change = callback

    def _emit_status(self, message: str):
        if self._on_status_change:
            self._on_status_change(message)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def refresh(self) -> List[Client]:
        """
        Scan for new Wizard101 clients not yet managed.

        Returns:
            List of newly discovered Client objects.
        """
        # Remove dead clients first
        self._handler.remove_dead_clients()
        new_clients = self._handler.get_new_clients()

        total = len(self._handler.clients)
        self._emit_status(f"Found {total} client(s)")
        return new_clients

    async def connect(self, client: Client) -> bool:
        """
        Activate hooks on the given client.

        Args:
            client: The Client to connect to.

        Returns:
            True if hooks were activated successfully.
        """
        if self._connected:
            await self.disconnect()

        try:
            self._emit_status("Activating hooks...")
            await client.activate_hooks()
            self._active_client = client
            self._connected = True
            self._emit_status(f"Connected to {client.title}")
            return True
        except Exception as e:
            self._emit_status(f"Connection failed: {e}")
            self._active_client = None
            self._connected = False
            return False

    async def disconnect(self):
        """Deactivate hooks and release the active client."""
        if self._active_client is not None:
            try:
                self._emit_status("Disconnecting...")
                await self._active_client.close()
                self._emit_status("Disconnected")
            except Exception as e:
                self._emit_status(f"Disconnect error: {e}")
            finally:
                self._active_client = None
                self._connected = False

    async def close(self):
        """Shut down completely — disconnect and close the handler."""
        await self.disconnect()
        await self._handler.close()

    def get_client_labels(self) -> List[str]:
        """Return human-readable labels for all tracked clients."""
        labels = []
        for i, client in enumerate(self._handler.clients):
            try:
                title = client.title or f"Client {i + 1}"
            except Exception:
                title = f"Client {i + 1}"
            labels.append(f"{i + 1}. {title} (PID {client.process_id})")
        return labels

    def get_client_by_index(self, index: int) -> Optional[Client]:
        """Get a client by its index in the tracked list."""
        clients = self._handler.clients
        if 0 <= index < len(clients):
            return clients[index]
        return None
