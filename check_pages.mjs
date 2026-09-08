// Исполнитель скрипта страницы для приёмки. Не набор проверок — сами
// проверки живут в check_lookup.py, здесь только среда.
//
// Три правила, от которых зависит, врёт заглушка или нет:
//
//  1. Элементов она НЕ ВЫДУМЫВАЕТ. Список id приходит из разметки,
//     разобранной bs4; чего в разметке нет — того нет и здесь.
//     Узлы, рождённые innerHTML (ряды таблицы проекта, поля «Констант»),
//     физически отсутствуют — и это правда, а не упрощение.
//  2. На неизвестный вызов DOM она БРОСАЕТ, а не возвращает пустышку.
//     Молчаливый undefined превратил бы проверку в проверку пустоты.
//     Брошенное исключение — сигнал дописать сюда пять строк.
//  3. Присвоенный innerHTML для неё непрозрачная строка: querySelectorAll
//     по такому узлу отдаёт пустой список. Сценариев, которые на это
//     опираются, в приёмке нет — а если появятся, они честно упадут.
//
// Вход и выход — по одному JSON: {script, dom, storage, responses, actions}
// на stdin, снимок состояния на stdout.

import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const input = JSON.parse(readFileSync(0, 'utf8'))
const throws = []

function element(id, spec) {
  const node = {
    id,
    // Браузер приводит присвоенное к строке: `el.value = 1500` даёт "1500".
    // Без этого проверка сравнивала число со строкой и краснела на
    // исправном коде.
    _value: spec.value == null ? '' : String(spec.value),
    get value() { return this._value },
    set value(v) { this._value = v == null ? '' : String(v) },
    checked: !!spec.checked,
    hidden: !!spec.hidden,
    disabled: false,
    // Поле только для чтения. Правило 1: если страница им пользуется,
    // а заглушка о нём не знает, снимок молча покажет прежнее — и
    // проверка «поля заперты» пройдёт на неисправном коде.
    readOnly: !!spec.readOnly,
    textContent: spec.text == null ? '' : String(spec.text),
    placeholder: spec.placeholder == null ? '' : String(spec.placeholder),
    dataset: { ...(spec.dataset || {}) },
    _html: null,
    _listeners: {},
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn) },
    removeEventListener() {},
    dispatchEvent(event) {
      for (const fn of this._listeners[event.type] || []) fn.call(this, event)
      return true
    },
    click() { this.dispatchEvent({ type: 'click', target: this }) },
    // <dialog>: окно выбора проекта открывается и закрывается этими
    // двумя. Заглушка держит только состояние — рисовать ей нечего.
    open: !!spec.open,
    showModal() { this.open = true },
    close() { this.open = false },
    focus() {}, select() {}, blur() {},
    remove() {},
    setAttribute() {}, getAttribute() { return null },
    insertAdjacentElement() {},
    append() {}, appendChild() {},
    closest() { return null },
    // Правило 3: назначенная разметка — строка, детей из неё не появляется.
    get innerHTML() { return this._html },
    set innerHTML(html) { this._html = String(html) },
    // Дети, которые есть В РАЗМЕТКЕ, готовит Python (например tbody у
    // таблицы позиций). Чего он не подготовил — того нет: правило 1.
    _children: spec.children || {},
    querySelector(sel) { return this._children[sel] || null },
    querySelectorAll(sel) { return this._children[sel] ? [this._children[sel]] : [] },
    get options() {
      throw new Error(`options у #${id}: заглушка не строит <option>, допишите check_pages.mjs`)
    },
    get classList() {
      return { add() {}, remove() {}, toggle() {}, contains() { return false } }
    },
    // Стиль хранится, а не выдаётся новым объектом каждый раз: полоса
    // хода работы задаётся шириной, и на выброшенном объекте правило
    // «полоса дошла до конца» нельзя было бы проверить.
    _style: {},
    get style() { return this._style },
  }
  return node
}

const nodes = {}
for (const [id, spec] of Object.entries(input.dom?.ids || {})) nodes[id] = element(id, spec)
// Подготовленные дети становятся такими же узлами — иначе innerHTML по
// ним не запишется, и отрисовка упадёт на «Cannot set properties of null».
for (const [id, spec] of Object.entries(input.dom?.ids || {})) {
  for (const [sel, name] of Object.entries(spec.children || {})) {
    nodes[id]._children[sel] = element(`${id}>${name}`, {})
  }
}

// Наборы по селекторам считает Python из разметки — здесь только раздача.
const selectors = {}
for (const [selector, ids] of Object.entries(input.dom?.selectors || {})) {
  selectors[selector] = ids.map((id) => nodes[id]).filter(Boolean)
}

const storage = { ...(input.storage || {}) }
const localStorage = {
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => { storage[k] = String(v) },
  removeItem: (k) => { delete storage[k] },
  clear: () => { for (const k of Object.keys(storage)) delete storage[k] },
  key: (i) => Object.keys(storage)[i] ?? null,
  get length() { return Object.keys(storage).length },
}

const fetches = []
async function fakeFetch(url, opts) {
  const body = opts && opts.body ? String(opts.body) : null
  fetches.push({ url: String(url), method: (opts && opts.method) || 'GET', body })
  const prepared = (input.responses || []).shift()
  if (!prepared) throw new Error(`нет заготовленного ответа на ${url}`)
  if (prepared.reject) throw new Error(prepared.reject)
  return {
    ok: prepared.ok !== false,
    status: prepared.status || (prepared.ok === false ? 500 : 200),
    json: async () => {
      if (prepared.notJson) throw new Error("Unexpected token '<'")
      return prepared.json
    },
    blob: async () => ({}),
    text: async () => prepared.text || '',
    headers: { get: () => null },
  }
}

// Замена документа целиком: готовая карточка приходит последней строкой
// потока разбора и выводится через document.write.
let written = ''
const document = {
  open() { written = '' },
  write(html) { written += String(html) },
  close() {},
  getElementById: (id) => nodes[id] || null,
  querySelector: (sel) => (selectors[sel] || [])[0] || null,
  querySelectorAll: (sel) => {
    if (!(sel in selectors)) {
      throws.push(`querySelectorAll("${sel}") — набор не подготовлен`)
      throw new Error(`селектор "${sel}" не подготовлен: допишите его в приёмку`)
    }
    return selectors[sel]
  },
  createElement: (tag) => element(`created:${tag}`, {}),
  addEventListener: () => {},
  body: element('body', {}),
}

const location = { href: '', assign(u) { this.href = String(u) } }
// В браузере window И ЕСТЬ глобальный объект: `window.AURRUM = …` в
// подключённом файле создаёт глобальное имя, которое видит скрипт
// страницы. Отдельный объект-заглушка этого не делал, и страница падала
// на «AURRUM is not defined» — то есть заглушка врала о самом устройстве
// среды, а не об одной подробности.
const context = {
  document, localStorage, location,
  fetch: fakeFetch,
  confirm: () => input.confirm !== false,
  alert: () => {},
  prompt: () => (input.prompt == null ? null : String(input.prompt)),
  setTimeout: (fn) => { if (input.runTimers) fn(); return 0 },
  clearTimeout: () => {},
  console: { log: () => {}, warn: () => {}, error: () => {} },
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} },
  Blob: class { constructor(parts) { this.parts = parts } },
  navigator: { clipboard: { writeText: async () => {} } },
  KeyboardEvent: class { constructor(t, o) { Object.assign(this, o); this.type = t } },
  Event: class { constructor(t) { this.type = t } },
  DataTransfer: class { constructor() { this.items = { add() {} }; this.files = [] } },
}
// Окно как слушатель: на нём висит предупреждение об уходе со
// страницы. Без этого страница падала бы на addEventListener, а правило
// «не уходи, не сохранив» осталось бы непроверяемым.
const windowListeners = {}
context.addEventListener = (type, fn) => { (windowListeners[type] ||= []).push(fn) }
// Прокрутка к началу после подстановки исполнения: страница длинная, и
// правки происходят выше места нажатия. Заглушке довольно знать, что
// такой вызов бывает.
context.scrollTo = () => {}
context.removeEventListener = () => {}
context.dispatchEvent = (event) => {
  for (const fn of windowListeners[event.type] || []) fn(event)
  return true
}

context.window = context
context.globalThis = context
context.self = context

const sandbox = vm.createContext(context)

let error = null
try {
  // Скрипты страницы идут по порядку: сначала общий модуль, затем свой.
  for (const piece of [].concat(input.script)) {
    vm.runInContext(piece, sandbox, { timeout: 5000 })
  }
  for (const action of input.actions || []) {
    const fn = new vm.Script(`(async () => { ${action} })()`)
    await fn.runInContext(sandbox, { timeout: 5000 })
  }
} catch (e) {
  error = e.message
}

const snapshot = {}
for (const [id, node] of Object.entries(nodes)) {
  snapshot[id] = { value: node.value, checked: node.checked, hidden: node.hidden,
                   text: node.textContent, disabled: node.disabled,
                   readOnly: node.readOnly, open: node.open,
                   style: node._style,
                   html: node._html == null ? null : node._html.length }
}

process.stdout.write(JSON.stringify({ storage, fetches, ids: snapshot, throws, error,
                                      written }))
