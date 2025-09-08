import asyncio
import keyboard
from wizwalker.client_handler import ClientHandler
from wizwalker.constants import Keycode
from teleporter import teleport_to_quest_position
from combat import combat_main

async def main():
    client_handler = ClientHandler()
    client = None

    try:
        # Wait for a client to appear
        while client is None:
            new_clients = client_handler.get_new_clients()
            if new_clients:
                client = new_clients[0]
                print(f"Found client: {client}")
            else:
                print("Waiting for Wizard101 client...")
                await asyncio.sleep(2)

        # Activate the hooks on the client
        try:
            await client.activate_hooks()
            print("Successfully activated hooks")
        except Exception as e:
            print(f"Failed to activate hooks: {e}")
            return

        # Main Loop
        print("Press 'q' and '1' simultaneously to exit.")
        while True:
            if keyboard.is_pressed('q') and keyboard.is_pressed('1'):
                print("\nShutdown signal received.")
                break
               
            if not await client.in_battle() and not await client.is_in_dialog():
                # Use the extracted teleport function
                await teleport_to_quest_position(client)

                if await client.is_in_npc_range():
                    await client.send_key(Keycode.X, 0)
            elif not await client.in_battle() and await client.is_in_dialog():
                await client.send_key(Keycode.SPACEBAR, 0)
            elif await client.in_battle() and not await client.is_in_dialog():
                async with client.mouse_handler:
                    await combat_main(client)
            
            await asyncio.sleep(0.1)
    finally:
        if client:
            print("Attempting to deactivate hooks...")
            await client.close()
            print("Hooks deactivated. Exiting.")

if __name__ == "__main__":
    asyncio.run(main())