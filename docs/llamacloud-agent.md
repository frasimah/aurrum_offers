# Агент LlamaCloud для техлистов

Настройка extraction-агента, который разбирает spec sheet производителя
в структуру, ложащуюся в карточку позиции без ручного дочитывания.

Деплой: `extract-product-specs-and-pricing-from-supplier-dd099`,
воркфлоу `process-file` (принимает `file_id`).

## Что менять и зачем

Агент собран по шаблону «каталог поставщика с ценами». На реальном техлисте
(TRUSSARDI VIBES) он отработал точно — сверка каждого токена с текстом PDF
расхождений не дала, — но форма ответа нам не подходит:

| Что не так | Почему мешает |
|---|---|
| Все исполнения слиты в одну строку `dimensions` | Главная ценность — привязка артикула к габаритам — заперта в свободном тексте, разбирать его снова регуляркой бессмысленно |
| Нет объёма в м³ | Именно он тянет 500 €/м³ транспорта в расчёте |
| Нет отделок с ролями | В рабочей книге это отдельные колонки |
| `category: "Bed"` | У нас фиксированный список типов на русском |
| Цены, лид-таймы, наличие | В техлистах брендов не встречаются никогда |

Главное изменение одно: **строкой массива становится исполнение, а не изделие**.
Кровать на пять размеров матраса — это пять записей, а не одна с текстовым
перечнем внутри.

## Схема

Живёт в репозитории: `config/extraction_schema.json`. Это единственный
источник правды — в интерфейс LlamaCloud схема вставляется оттуда, а не
наоборот. Публичный API деплоя доступа к его конфигу не даёт (у деплоя
только эндпоинты запуска), поэтому синхронизация ручная: правим файл,
вставляем в агента.

Схема на английском намеренно: документы приходят на английском и итальянском,
и описания полей на языке источника работают надёжнее. Русские значения только
там, где это наш фиксированный список — `type_ru` и `role_ru`.

## Инструкция агенту

Вставить в поле system prompt / instructions:

```
You extract data from furniture and lighting spec sheets for an interior
design studio. The extracted values go straight into a commercial offer,
so a wrong number costs real money. Accuracy matters far more than
completeness.

RULES

1. COPY, DO NOT REWRITE. Every value except `type_ru` and `role_ru` is
   copied from the document as printed. Do not translate, do not
   normalise case, do not reformat dates, do not tidy up punctuation,
   do not convert units, do not reorder numbers inside a dimensions
   string.

2. ONE ENTRY PER EXECUTION. If the document lists several sizes,
   versions or article codes of the same model, each one is its own
   entry in `variants`, with its own `sku` and its own `dims_raw`.
   Never join two executions into one string. This is the single most
   important rule.

3. CENTIMETRES ONLY. These documents usually print each size twice —
   once in centimetres, once in inches, often on the next line and with
   fractions such as "79 2/3x95x36 1/3H.". Take the centimetre line and
   ignore the inch line completely.

4. NULL BEATS A GUESS. If the document does not state something, the
   value is null. Never infer a dimension from a drawing, never derive
   volume from dimensions, never fill a field from general knowledge of
   the brand. An empty answer is a correct answer; an invented one is
   a defect.

5. NO PRICES. Ignore prices, currencies, discounts, lead times and
   stock availability even when the document contains them. They are
   not part of this task.

6. IGNORE ASSEMBLY AND CARE. Mounting instructions, cleaning advice,
   packaging notes and legal disclaimers are not extracted.

7. MATERIAL NAMES STAY IN THE ORIGINAL LANGUAGE. "NOCE CANALETTO"
   stays "NOCE CANALETTO". Only the role of the finish is mapped to the
   Russian list.

WHAT A GOOD ANSWER LOOKS LIKE

For a bed offered in five mattress sizes, `variants` has five entries:

  sku "VBE (LE1)"  dims_raw "202x241x92H."  variant_note "165x200"
  sku "VBE (LE2)"  dims_raw "222x241x92H."  variant_note "185x200"
  sku "VBE (QUSA)" dims_raw "189x246x92H."  variant_note "152x205"
  ...

not one entry whose dims_raw lists all five.
```

## Что остаётся на стороне Python

Модели не отдаётся ничего, что можно посчитать. Это правило уже дважды себя
оправдало: на одной и той же странице извлечение раскладывало Д/Г/В по-разному
от запуска к запуску, а набор чисел при этом был верный.

- **Оси Д/Г/В** — `parse_dims()` разбирает `dims_raw` по нотации источника.
- **Объём** — берётся `packed_volume_m3`, если производитель его напечатал;
  иначе считается формулой книги `ROUNDUP(Д×Г×В×1,5/1e6; 1)`. В карточке
  видно, какой источник сработал.
- **Сверка с первоисточником** — названия материалов проверяются на наличие
  в тексте документа; чего там нет, убирается с предупреждением.

## Как проверить, что схема встала правильно

Прогнать техлист VIBES:
`https://luxurylivinggroup.com/cdn/shop/files/VIBES_bed_GUEST.pdf`

Ожидается ровно пять записей в `variants` со своими `sku` и `dims_raw`:

| sku | dims_raw | variant_note |
|---|---|---|
| VBE (LE1) | 202x241x92H. | 165x200 |
| VBE (LE2) | 222x241x92H. | 185x200 |
| VBE (QUSA) | 189x246x92H. | 152x205 |
| VBE (KUSA) | 232x246x92H. | 195x205 |
| VBE (LE7) | 237x241x92H. | 200x200 |

Первая строка — та самая позиция R22 из рабочей книги, по ней и сверяем.

Признаки, что что-то не так:
- одна запись в `variants` вместо пяти — правило 2 не дошло;
- в `dims_raw` появились дроби (`79 2/3x95x36 1/3H.`) — взята дюймовая
  строка, правило 3;
- `packed_volume_m3` заполнен — в этом документе объёма нет, значит
  посчитан самостоятельно, правило 4.

## Особенность деплоя

Первые запросы к спящему деплою отвечают 502 и 504 примерно полминуты —
это холодный старт, а не ошибка. При встраивании нужен ретрай, иначе
менеджер получает ошибку на ровном месте.

Клиент из примера в документации устарел: вместо `from workflows.client
import WorkflowClient` актуально `from llama_agents.client import ...`
(пакет `llama-agents-client`).
