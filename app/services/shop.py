from dataclasses import dataclass, field

from sqlalchemy import select

from app.database.models import FAQ, Product

WELCOME = (
    "Здравствуйте! Я помощник магазина электроники SupportAI.\n\n"
    "Помогу выбрать товар, узнать цену и наличие, разобраться с доставкой, оплатой и возвратом. "
    "Выберите раздел ниже или напишите вопрос своими словами.\n\n"
    "Например: «Есть ли доставка в Москву?» или «Какие наушники есть в наличии?»\n\n"
    "Это демо-магазин для портфолио: товары и цены учебные, покупки не оформляются."
)
HELP = (
    "Что можно сделать:\n/catalog — посмотреть товары\n/delivery — доставка\n"
    "/payment — оплата\n/returns — возврат\n/operator — обратиться к человеку\n"
    "/bot — завершить обращение и вернуться к боту\n/menu — открыть разделы\n\n"
    "Можно писать обычным текстом и задавать уточнения о выбранном товаре. "
    "Не присылайте пароли, коды оплаты и данные карты."
)


@dataclass
class ShopReply:
    answer: str
    choices: list[dict] = field(default_factory=list)
    subject: str | None = None
    can_vote: bool = False


def price_text(price):
    return f"{price:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


async def shop_reply(session, text):
    value = text.strip().casefold().rstrip("?!.,")
    if value == "/continue":
        return ShopReply("Продолжим. Что уточнить? Напишите вопрос — наша переписка сохранена.")
    if value in {"/help", "/menu", "что ты умеешь", "что можешь рассказать", "че можешь рассказать"}:
        return ShopReply(HELP)
    if value in {"привет", "здравствуйте", "добрый день", "добрый вечер", "ты тут", "че тут"}:
        return ShopReply(WELCOME)
    if value in {
        "какой у вас магазин",
        "что вы продаете",
        "что вы продаёте",
        "что у вас есть в магазине",
        "/catalog",
    }:
        categories = list(
            (await session.scalars(select(Product.category).distinct().order_by(Product.category))).all()
        )
        return ShopReply(
            "В каталоге: " + ", ".join(categories) + ".\nВыберите категорию.\nТовары и цены демонстрационные."
            if categories
            else "Каталог пока пуст. Можно уточнить ассортимент у оператора через /operator.",
            [
                {"text": c, "action": "/category " + c}
                for c in categories
                if len(("shop:/category " + c).encode()) <= 64
            ][:12],
        )
    if value.startswith("/category "):
        category = text.strip().split(" ", 1)[1]
        products = list(
            (
                await session.scalars(
                    select(Product).where(Product.category == category).order_by(Product.id).limit(10)
                )
            ).all()
        )
        return ShopReply(
            f"{category}: выберите товар, чтобы увидеть цену, наличие и характеристики."
            if products
            else "В этой категории пока нет товаров. Откройте /catalog.",
            [
                {"text": f"{p.name} — {price_text(p.price)}"[:60], "action": f"/product {p.id}"}
                for p in products
            ],
        )
    if value.startswith("/product "):
        try:
            product_id = int(value.split(" ", 1)[1])
        except ValueError:
            return ShopReply("Откройте /catalog и выберите товар кнопкой.")
        p = await session.get(Product, product_id)
        if p is None:
            return ShopReply("Этот товар больше недоступен. Откройте /catalog.")
        stock = f"В наличии: {p.stock} шт." if p.stock else "Сейчас нет в наличии."
        specs = "\n".join(f"{k}: {v}" for k, v in p.specifications.items() if k != "демонстрационные_данные")
        return ShopReply(
            f"{p.name}\nЦена: {price_text(p.price)}\n{stock}\n{specs}"[:2600]
            + "\n\nМожно задать уточняющий вопрос об этом товаре. Для покупки в демо оплата недоступна.",
            subject=f"Расскажи о товаре {p.name}",
            can_vote=True,
        )
    section = {"/delivery": "Доставка", "/payment": "Оплата", "/returns": "Возврат"}.get(value)
    if section:
        rows = (
            await session.scalars(select(FAQ).where(FAQ.category == section).order_by(FAQ.id).limit(8))
        ).all()
        return ShopReply(
            section + "\n\n" + "\n\n".join(f"{r.question}\n{r.answer}" for r in rows)[:2800]
            if rows
            else "Информация пока не заполнена. Уточните у оператора через /operator.",
            can_vote=bool(rows),
        )
    # Exact FAQ questions are answered from the current database, even during an AI outage.
    faqs = (await session.scalars(select(FAQ).order_by(FAQ.id).limit(200))).all()
    for faq in faqs:
        if faq.question.strip().casefold().rstrip("?!.,") == value:
            return ShopReply(faq.answer[:3000], can_vote=True)
    return None
