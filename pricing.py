# -*- coding: utf-8 -*-
"""
Расчёт цены позиции — та же цепочка, что в скрытых колонках книги.

Восстановлена из рабочего файла `0000-Offer-…-AUR-FORM.xlsx` и сверена
с ним по числам: на всех пяти позициях расчёт сходится с колонкой AE,
а клиентская цена в колонке E оказалась её округлением до десятков.

Здесь нет модели и не может быть: это место, где ошибка стоит прямых
денег. Всё арифметика.

    W  = T × (1 − скидка фабрики)      цена со скидкой
    Y  = W × (1 + наценка дилера)      ЗАКУП AURRUM
    Z  = Y × 35 %                      РЕНТАБ
    AA = W × 5 %                       ТРАНШ
    AB = 200                           SWIFT, фиксированно за позицию
    AC = объём м³ × 500                ТРАНСПОРТ
    AD = Y + Z + AA + AB + AC          СУММА
    AE = AD ÷ коэффициент сборки       СУМ СО СБОРКОЙ  ← из неё цена клиенту
    затем ÷(1−10 %) ÷(1−6 %) ÷(1−5 %) ÷(1−18 %)
    ДИЗАЙНЕР, УСНО, НДС, FINSERV — уровни цены под разные условия оплаты
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Ставки лежат в первой строке книги. Здесь — значения по умолчанию;
# в компред они снимаются один раз, иначе прошлогодний расчёт пересчитается
# по сегодняшним ставкам и разойдётся с тем, что клиент держит на руках.
DEFAULT_RATES = {
    "margin": 35.0,        # Z  рентабельность, % от закупа
    "transfer": 5.0,       # AA транш, % от цены со скидкой
    "swift": 200.0,        # AB фиксированная комиссия за позицию, евро
    "freight": 500.0,      # AC транспорт, евро за м³
    "designer": 10.0,      # AF
    "usno": 6.0,           # AG
    "vat": 5.0,            # AH
    "finserv": 18.0,       # AI  безнал
}

# Три величины у каждой позиции свои, но начинаются с одних и тех же
# чисел — снято с рабочей формы, строки 16, 18, 20, 22, 23:
#
#   скидка фабрики   0,45  0,5  0,5  0,5  0,5   -> норма 0,5
#   наценка дилера   0     0,12 0    0    0     -> норма 0
#   сборка           1     1    0,95 0,95 1     -> норма 1
#
# Подставляются в новую позицию, чтобы менеджер правил отклонения, а не
# набирал одно и то же пять раз. Пустое поле — это не «нет значения»,
# а осознанный ноль: очищенная скидка означает работу без скидки.
DEFAULT_POSITION = {
    "factory_discount": 0.5,   # V  скидка фабрики
    "dealer_markup": 0.0,      # X  наценка дилера
    "assembly": 1.0,           # делитель AE, коэффициент сборки
}

# Цена клиенту — округлённая AE, но правила округления нет: в книге
# встречаются 5752,2 -> 5750 (вниз), 2194,7 -> 2200 (вверх) и
# 15593,7 -> 15590 (вниз). Это решение менеджера, а не формула, поэтому
# считаем до десятков и отдаём как предложение, которое можно поправить.
PRICE_STEP = 10


@dataclass
class Line:
    """Расчёт одной позиции. Пустые поля означают «нечего считать»."""
    purchase: float = 0.0        # Y  ЗАКУП
    margin: float = 0.0          # Z  РЕНТАБ
    transfer: float = 0.0        # AA ТРАНШ
    swift: float = 0.0           # AB SWIFT
    freight: float = 0.0         # AC ТРАНСПОРТ
    total: float = 0.0           # AD СУММА
    with_assembly: float = 0.0   # AE СУМ СО СБОРКОЙ
    price: float = 0.0           # предложение цены клиенту: округлённая AE
    levels: dict = field(default_factory=dict)   # ДИЗАЙНЕР / УСНО / НДС / безнал


def _num(value, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return default


def line(list_price, volume_m3, *, factory_discount=None, dealer_markup=None,
         assembly=None, qty=1, rates: dict | None = None) -> Line:
    """Цена прайса и объём -> вся цепочка книги.

    Не заданное поле берёт значение из `DEFAULT_POSITION`, пустая строка —
    ноль. Разница существенная: у позиции, которую ещё не трогали, скидка
    фабрики должна быть обычные 50 %, а у очищенной руками — её нет.
    """
    r = {**DEFAULT_RATES, **(rates or {})}

    t = _num(list_price)
    if t <= 0:
        return Line()

    def own(value, key):
        return DEFAULT_POSITION[key] if value is None else _num(value)

    discounted = t * (1 - own(factory_discount, "factory_discount"))
    purchase = discounted * (1 + own(dealer_markup, "dealer_markup"))

    margin = purchase * r["margin"] / 100
    transfer = discounted * r["transfer"] / 100
    swift = r["swift"]
    freight = _num(volume_m3) * r["freight"]
    total = purchase + margin + transfer + swift + freight

    coefficient = own(assembly, "assembly") or 1.0
    with_assembly = total / coefficient

    levels = {}
    running = with_assembly
    for key in ("designer", "usno", "vat", "finserv"):
        running = running / (1 - r[key] / 100)
        levels[key] = round(running, 2)

    return Line(
        purchase=round(purchase, 2),
        margin=round(margin, 2),
        transfer=round(transfer, 2),
        swift=round(swift, 2),
        freight=round(freight, 2),
        total=round(total, 2),
        with_assembly=round(with_assembly, 2),
        # Предложение, а не истина: правила округления в книге нет.
        price=round(with_assembly / PRICE_STEP) * PRICE_STEP,
        levels=levels,
    )


def project(positions: list[dict], rates: dict | None = None) -> dict:
    """Позиции проекта -> расчёт по каждой и итоги."""
    lines, total_sum, total_volume = [], 0.0, 0.0
    levels_sum: dict[str, float] = {}
    for p in positions:
        qty = max(1, int(_num(p.get("qty"), 1)))
        computed = line(
            p.get("list_price"), p.get("volume_m3"),
            factory_discount=p.get("factory_discount"),
            dealer_markup=p.get("dealer_markup"),
            assembly=p.get("assembly"),
            rates=rates,
        )
        lines.append({
            "purchase": computed.purchase, "margin": computed.margin,
            "transfer": computed.transfer, "swift": computed.swift,
            "freight": computed.freight, "total": computed.total,
            "with_assembly": computed.with_assembly,
            "price": computed.price, "sum": round(computed.price * qty, 2),
            "levels": computed.levels,
            "qty": qty,
        })
        total_sum += computed.price * qty
        total_volume += _num(p.get("volume_m3")) * qty
        # Уровни цены тоже умножаем на количество — иначе итог «безнал»
        # считается за штуку, а «сумма» за всё, и они не сходятся.
        for key, value in computed.levels.items():
            levels_sum[key] = levels_sum.get(key, 0.0) + value * qty

    return {
        "lines": lines,
        "sum": round(total_sum, 2),
        "volume_m3": round(total_volume, 2),
        "count": len(positions),
        "levels": {k: round(v, 2) for k, v in levels_sum.items()},
    }
