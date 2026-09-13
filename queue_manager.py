import asyncio
from dataclasses import dataclass
from typing import Optional


@dataclass
class QueueItem:

    user_id: int
    chat_id: int
    url: str
    message_id: int

    status: str = "waiting"

    file_name: Optional[str] = None
    error: Optional[str] = None

    file_type: Optional[str] = None
    file_size: Optional[int] = None
    history_id: Optional[int] = None
    caption: Optional[str] = None

    force_download: bool = False

    task: Optional[asyncio.Task] = None


class UploadQueue:

    def __init__(self):

        self.queue = asyncio.Queue()

        self.items = {}

        self.counter = 0

        self.worker_task = None

        self.running = False

        self.process_callback = None

    async def start(self):

        if self.running:
            return

        self.running = True

        self.worker_task = asyncio.create_task(
            self.worker()
        )

    async def add(
        self,
        user_id: int,
        chat_id: int,
        url: str,
        message_id: int,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        force_download: bool = False,
    ):

        self.counter += 1

        item_id = self.counter

        item = QueueItem(
            user_id=user_id,
            chat_id=chat_id,
            url=url,
            message_id=message_id,
            caption=caption,
            file_name=file_name,
            force_download=force_download,
        )

        self.items[item_id] = item

        await self.queue.put(
            (item_id, item)
        )

        return item_id

    async def worker(self):

        while self.running:

            try:

                item_id, item = await self.queue.get()

                if item.status == "cancelled":

                    self.queue.task_done()

                    continue

                item.status = "processing"

                try:

                    if self.process_callback:

                        current_task = asyncio.current_task()

                        item.task = current_task

                        await self.process_callback(
                            item_id,
                            item,
                        )

                    else:

                        item.status = "failed"

                        item.error = (
                            "No process callback configured."
                        )

                except asyncio.CancelledError:

                    item.status = "cancelled"

                    item.error = "Task cancelled."

                except Exception as error:

                    item.status = "failed"

                    item.error = str(error)

                finally:

                    item.task = None

                    self.queue.task_done()

            except asyncio.CancelledError:

                break

            except Exception:

                await asyncio.sleep(1)

    def cancel(self, item_id: int) -> bool:

        item = self.items.get(item_id)

        if not item:
            return False

        if item.status in (
            "completed",
            "failed",
            "cancelled",
        ):
            return False

        if item.status == "waiting":

            item.status = "cancelled"

            item.error = (
                "Cancelled while waiting in queue."
            )

            return True

        if item.task:

            item.status = "cancelled"

            item.error = "Cancelled by user."

            item.task.cancel()

            return True

        return False

    def get(self, item_id: int):

        return self.items.get(item_id)

    def get_position(self, item_id: int):

        item = self.items.get(item_id)

        if not item:
            return None

        if item.status != "waiting":
            return 0

        position = 0

        for current_id, current_item in self.items.items():

            if current_item.status == "waiting":
                position += 1

            if current_id == item_id:
                return position

        return None

    def size(self):

        return self.queue.qsize()

    def get_active_item(self):

        for item_id, item in self.items.items():

            if item.status in (
                "processing",
                "uploading",
            ):

                return item_id, item

        return None, None

    async def stop(self):

        self.running = False

        if self.worker_task:

            self.worker_task.cancel()

            try:

                await self.worker_task

            except asyncio.CancelledError:

                pass

            self.worker_task = None

        self.process_callback = None