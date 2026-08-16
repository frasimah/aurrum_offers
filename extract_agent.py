# -*- coding: utf-8 -*-
"""
Техлист -> исполнения для карточки.

Разбор делает `llama_extract`; здесь остаётся то, что модели не отдаётся:
раскладка осей, объём и сверка значений с нашими списками. Списки нужны
потому, что LlamaExtract не соблюдает `enum` в схеме — он молча возвращает
последнее значение списка, из-за чего кровать становилась «Ковёр».
"""

from __future__ import annotations

import llama_extract
import product_lookup as pl


def extract_pdf(url: str) -> dict:
    """Ссылка на техлист -> извлечённые данные по нашей схеме."""
    return llama_extract.from_pdf_url(url)


def to_candidates(data: dict) -> tuple[list[dict], list[dict], list[str]]:
    """Данные извлечения -> исполнения, отделки, предупреждения.

    Габариты и объём считает Python: набор чисел извлечение отдаёт верный,
    а по осям раскладывает нестабильно.
    """
    warnings: list[str] = []
    candidates: list[dict] = []
    finishes: list[dict] = []
    brand = (data.get("brand") or "").strip()

    for product in data.get("products") or []:
        model = (product.get("model") or "").strip()

        type_ru, warning = pl.normalize_type(product.get("type_ru") or "")
        if warning:
            warnings.append(warning)

        for f in product.get("finishes") or []:
            material = (f.get("material") or "").strip()
            if not material:
                continue
            role, warning = pl.normalize_role(f.get("role_ru") or "")
            if warning and warning not in warnings:
                warnings.append(warning)
            finishes.append({"role_ru": role, "material": material,
                             "code": (f.get("code") or "").strip() or None})

        for v in product.get("variants") or []:
            dims_raw = (v.get("dims_raw") or "").strip()
            if not dims_raw:
                continue
            w, d, h, sure = pl.parse_dims(dims_raw, type_ru)

            declared = v.get("packed_volume_m3")
            if isinstance(declared, (int, float)) and declared > 0:
                volume, source = round(float(declared), 2), "производитель"
            else:
                volume, source = pl.volume_m3(w, d, h), "формула"

            candidates.append({
                "sku": (v.get("sku") or "").strip() or None,
                "value": dims_raw,
                "context": (v.get("variant_note") or "").strip(),
                "width_cm": w, "depth_cm": d, "height_cm": h,
                "volume_m3": volume, "volume_source": source,
                "dims_confident": sure,
                "brand": brand, "model": model, "type_ru": type_ru,
            })

        if not (product.get("variants") or []):
            warnings.append(f"У «{model or 'позиции'}» не нашлось ни одного исполнения с габаритами.")

    if not candidates:
        warnings.append("Габаритов в документе не нашлось — заполните вручную.")
    return candidates, finishes, warnings
