"""Разбор текста уведомления банка на сервере (порт frontend/smsparser.js). Текст не сохраняется и не логируется."""
import re
from datetime import date

KEYWORDS = {
    "expense": [
        ("Коммунальные услуги", r"коммунал|электро|энерго|водоканал|теплосет|отоплен|вывоз мусор|тазалык|utilit"),
        ("Связь и интернет", r"beeline|билайн|megacom|мегаком|nur ?telecom|нур ?телеком|интернет|internet|saima|сайма|мобильн|телеком"),
        ("Жильё", r"аренд|квартир|ипотек|\brent|жилье|жильё"),
        ("Учеба", r"универс|институт|школ|лицей|курс|обучен|учеб|tuition|udemy|coursera|книг|канцеляр|академ"),
        ("Здоровье", r"аптек|pharm|фарм|клиник|больниц|врач|стомат|медицин|лаборатор"),
        ("Транспорт", r"такси|taxi|yandex ?go|яндекс ?го|indrive|jeti|азс|бензин|petrol|газпром|нефтегаз|маршрутк|автобус|метро|проезд|парковк|parking|uber|транспорт"),
        ("Еда", r"продукт|супермаркет|market|маркет|globus|глобус|народный|фрунзе|магнум|кафе|ресторан|restaurant|coffee|кофе|пицц|pizza|бургер|burger|kfc|обед|столов|пекарн|булочн|bakery|navat|dodo|додо|шаурм|food"),
        ("Одежда", r"одежд|обувь|zara|lcw|defacto|sela|fashion|куртк"),
        ("Развлечения", r"кино|cinema|netflix|spotify|steam|игр|клуб|боулинг|концерт|театр|youtube|apple music"),
    ],
    "income": [
        ("Зарплата", r"зарплат|salary|оклад|аванс|заработн"),
        ("Стипендия", r"стипенд"),
        ("Подработка", r"фриланс|freelance|подработ|гонорар"),
    ],
}
NUM = r"(\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
CUR_AFTER = r"(сом(?:ов)?|kgs|som|с\.|usd|\$|eur|€|руб\.?|rub|₽|kzt|₸)(?![a-zа-я])"
EXP = re.compile(r"списан|списание|оплат|покупк|снят|выдач|перевод на|purchase|payment|withdraw|debit|платеж|платёж|расход|spent", re.I)
INC = re.compile(r"зачислен|поступлен|пополнен|получен|перевод от|credit|deposit|received|зарплат", re.I)
SKIP_CTX = re.compile(r"баланс|остаток|доступн|balance|avail|лимит", re.I)


def category_name(text: str, type_: str) -> str:
    for name, pat in KEYWORDS[type_]:
        if re.search(pat, text, re.I):
            return name
    return "Прочее" if type_ == "expense" else "Прочие доходы"


def to_number(s: str) -> float:
    t = re.sub(r"[\u00a0\s]", "", s)
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    else:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


def to_iso_date(s: str) -> date | None:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})", s)
        if m:
            y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return date(y, int(m[2]), int(m[1]))
    except ValueError:
        pass
    return None


def parse_sms(msg: str, today: date) -> tuple[str, dict | None]:
    """Возвращает ('ok', данные) | ('ignored', None) | ('foreign', None)."""
    raw = re.sub(r"\s+", " ", msg).strip()
    t = re.sub(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}", " ", raw)
    t = re.sub(r"\d{1,2}:\d{2}(:\d{2})?", " ", t)
    t = re.sub(r"\*+\s*\d{2,4}|\d{4}\s*\*+\s*\d{0,4}", " ", t)
    amount, cur = 0.0, ""
    for m in re.finditer(NUM + r"\s*" + CUR_AFTER, t, re.I):
        if SKIP_CTX.search(t[max(0, m.start() - 16): m.start()]):
            continue
        amount, cur = to_number(m[1]), m[2]
        break
    if not amount:
        for m in re.finditer(r"(kgs|usd|eur|\$|€|сом)\s*" + NUM, t, re.I):
            if SKIP_CTX.search(t[max(0, m.start() - 16): m.start()]):
                continue
            amount, cur = to_number(m[2]), m[1]
            break
    if not amount:
        return "ignored", None  # например, код подтверждения
    if not re.fullmatch(r"сом(ов)?|kgs|som|с\.", cur, re.I):
        return "foreign", None
    e, i = EXP.search(t), INC.search(t)
    if not e and not i:
        return "ignored", None
    type_ = "income" if i and (not e or i.start() < e.start()) else "expense"
    d = re.search(r"(?:(?:^|\s)в|\bat\b|merchant|торговая точка|магазин|получатель|описание)[:\s]+([^.;,]{2,40})", t, re.I)
    description = (d[1] if d else t).strip()[:80]
    return "ok", {"amount": amount, "type": type_, "description": description,
                  "occurred_on": to_iso_date(raw) or today, "category": category_name(t, type_)}
