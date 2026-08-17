# -*- coding: utf-8 -*-
"""
Карточка -> строка рабочей книги.

Соединяет две готовые половины: сбор данных о позиции и книгу, в которой
менеджер считает. Базы для этого не нужно — строка кладётся в буфер обмена
и вставляется в книгу одним движением.

Формулы собираются под конкретный номер строки, поэтому вставлять надо
именно в ту строку, которую указали, — иначе ссылки уедут.

Разметка колонок взята из рабочего файла `0000-Offer-…-AUR-FORM.xlsx`:

    A №   B Производитель   C Описание   D К-во   E Цена   F Сумма
    G СПЕЦ.ЦЕНА (скрыта)    H СПЕЦ.СУММА  I Фото   J Д  K Г  L В
    M N O Отделка 1-3       P Примечание  Q Схема  R м3 (скрыта)  S м3 всего

Скрытая часть T..AQ — расчёт цены. Она выключена по умолчанию: в книге
эти формулы уже стоят, а коэффициент сборки в AE у каждой позиции свой
(встречались /1, /0.95 и /0.9), и затирать его вслепую нельзя.
"""

from __future__ import annotations

# Порядок колонок видимой части. None — ячейка, которую заполняет менеджер
# или которая содержит картинку.
VISIBLE = "ABCDEFGHIJKLMNOPQRS"


def _cell(value) -> str:
    """Ячейка в вид, который Excel примет из буфера.

    Описание многострочное, а в TSV перевод строки разрывает строку —
    поэтому такие ячейки берём в кавычки, как это делает сам Excel.
    """
    if value is None:
        return ""
    text = str(value)
    if any(ch in text for ch in ('"', "\t", "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


def visible_row(fields: dict, row: int) -> list[str]:
    """Колонки A..S для указанной строки книги."""
    r = int(row)
    return [
        fields.get("number") or "",                  # A  №
        fields.get("brand") or "",                   # B  Производитель
        fields.get("description") or "",             # C  Описание
        fields.get("qty") or 1,                      # D  К-во
        fields.get("price") or "",                   # E  Цена, Евро
        f"=D{r}*E{r}",                               # F  Сумма
        "",                                          # G  СПЕЦ. ЦЕНА
        f"=D{r}*G{r}",                               # H  СПЕЦ. СУММА
        "",                                          # I  Фото — вставляется картинкой
        fields.get("width_cm") or "",                # J  Д, см
        fields.get("depth_cm") or "",                # K  Г, см
        fields.get("height_cm") or "",               # L  В, см
        0, 0, 0,                                     # M N O  Отделка 1-3
        fields.get("note") or "",                    # P  Примечание
        "",                                          # Q  Схема
        f"=U{r}",                                    # R  м3
        f"=R{r}*D{r}",                               # S  м3 всего
    ]


def pricing_row(fields: dict, row: int) -> list[str]:
    """Скрытый расчёт T..AI: то, что в книге считает цену.

    Руками остаются цена прайса (T), скидка фабрики (V) и наценка
    дилера (X) — их со страницы бренда не узнать.
    """
    r = int(row)
    return [
        fields.get("list_price") or "",               # T  цена прайса
        f"=ROUNDUP(J{r}*K{r}*L{r}*1.5/1000000,1)",    # U  объём м3
        fields.get("factory_discount") or "",         # V  скидка фабрики
        f"=T{r}-T{r}*V{r}",                           # W  цена со скидкой
        fields.get("dealer_markup") or 0,             # X  наценка дилера
        f"=W{r}+W{r}*X{r}",                           # Y  ЗАКУП AURRUM
        f"=Y{r}*($Z$1+0)/100",                        # Z  РЕНТАБ
        f"=W{r}*$AA$1/100",                           # AA ТРАНШ
        # В форме SWIFT стоит числом в каждой строке, а не ссылкой на
        # первую, — поэтому подставляем сюда заданную величину.
        fields.get("swift", 200),                     # AB SWIFT
        f"=U{r}*$AC$1",                               # AC ТРАНСПОРТ
        f"=Y{r}+Z{r}+AA{r}+AB{r}+AC{r}",              # AD СУММА
        f"=AD{r}/{fields.get('assembly') or 1}",      # AE СУМ СО СБОРКОЙ
        f"=AE{r}/(1-$AF$1/100)",                      # AF ДИЗАЙНЕР
        f"=AF{r}/(1-$AG$1/100)",                      # AG УСНО
        f"=AG{r}/(1-$AH$1/100)",                      # AH НДС
        f"=AH{r}/(1-$AI$1/100)",                      # AI FINSERV / безнал
    ]


def build(fields: dict, row: int, with_pricing: bool = False) -> str:
    """Строка для вставки в книгу: ячейки через табуляцию."""
    cells = visible_row(fields, row)
    if with_pricing:
        # Между S и T в книге пусто не бывает — колонки идут подряд.
        cells += pricing_row(fields, row)
    return "\t".join(_cell(c) for c in cells)
