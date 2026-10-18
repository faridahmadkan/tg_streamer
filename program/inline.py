from pyrogram import Client, errors
from pyrogram.types import InlineQuery, InlineQueryResultArticle, InputTextMessageContent
from program.music import ytsearch_all


@Client.on_inline_query()
async def inline(client: Client, query: InlineQuery):
    answers = []
    search_query = (query.query or "").strip()
    if not search_query:
        await client.answer_inline_query(
            query.id, results=[], switch_pm_text="type a song name to search music sources",
            switch_pm_parameter="help", cache_time=0,
        )
        return
    try:
        results = ytsearch_all(search_query, 10)
        for title, url, duration, thumbnail, provider in results:
            answers.append(InlineQueryResultArticle(
                title=title[:128],
                description=(f"{provider} · {duration}" if duration else provider)[:255],
                input_message_content=InputTextMessageContent(url),
                thumb_url=thumbnail or None,
            ))
        await query.answer(results=answers, cache_time=0)
    except errors.QueryIdInvalid:
        return
    except Exception:
        try:
            await query.answer(results=[], cache_time=0)
        except Exception:
            pass
