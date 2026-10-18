from typing import List
from pyrogram.types import Chat
from pyrogram.enums import ChatMembersFilter, ChatMemberStatus
from cache.admins import get as gett, set


async def get_administrators(chat: Chat) -> List[int]:
    get = gett(chat.id)

    if get:
        return get
    else:
        to_set = []
        async for administrator in chat.get_members(
            filter=ChatMembersFilter.ADMINISTRATORS
        ):
            if administrator.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
                to_set.append(administrator.user.id)

        set(chat.id, to_set)
        return to_set
