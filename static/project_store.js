// Черновик проекта в браузере.
//
// Работа менеджера держится здесь между нажатиями «Сохранить»: черновик
// пишется на каждую правку и ничего не теряет, а на сервер проект уходит
// явным действием. Автосохранения нет намеренно — каждая запись
// переписывает общий список проектов, а он доходит с задержкой, и
// частые записи съедали бы чужие строки.
//
// Ключ у каждого проекта свой. Раньше было четыре общих слота
// (aurrum.project / header / final), и открытый проект B считался бы
// скидками проекта A, а несохранённая правка A молча пропадала.
window.AURRUM = (function () {
  const CURRENT = 'aurrum.current';          // id открытого проекта или 'new'
  const DRAFT = 'aurrum.draft.';             // + id -> целое состояние
  const RATES = 'aurrum.rates';              // ставки: одни на все проекты
  const POS_DEFAULTS = 'aurrum.position_defaults';

  const read = (key, fallback) => {
    try { const v = JSON.parse(localStorage.getItem(key)); return v == null ? fallback : v; }
    catch (e) { return fallback; }
  };
  const write = (key, value) => {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* приватный режим */ }
  };

  const currentId = () => localStorage.getItem(CURRENT) || 'new';
  const setCurrent = (id) => localStorage.setItem(CURRENT, id);

  const empty = () => ({ positions: [], header: {}, final: {}, rates: null, rev: 0 });

  function draft(id) {
    const state = read(DRAFT + (id || currentId()), null);
    if (state) return { ...empty(), ...state };
    // Разовый перенос со старых общих ключей: работа, набранная до
    // появления проектов на сервере, не должна пропасть. Старые ключи
    // не удаляем — откат не потеряет её ещё раз.
    const old = read('aurrum.project', null);
    if (old && old.length) {
      return { ...empty(), positions: old,
               header: read('aurrum.header', {}), final: read('aurrum.final', {}) };
    }
    return empty();
  }

  const saveDraft = (id, state) => write(DRAFT + (id || currentId()), state);
  const dropDraft = (id) => localStorage.removeItem(DRAFT + id);

  // Опознаватель делает браузер и запоминает ДО отправки: потерянный
  // ответ сервера тогда не плодит второй такой же проект.
  function newId() {
    const rnd = Math.random().toString(16).slice(2, 10);
    const day = new Date().toISOString().slice(0, 10).replace(/-/g, '');
    return `${day}-${rnd}`;
  }

  const rates = () => read(RATES, undefined);
  const positionDefaults = () => ({
    factory_discount: 0.5, dealer_markup: 0, assembly: 1, ...read(POS_DEFAULTS, {}),
  });

  // Позицию кладут четыре страницы — правило одно и живёт здесь.
  function addPosition(position) {
    const id = currentId();
    const state = draft(id);
    state.positions = (state.positions || []).concat([
      { ...position, qty: 1, ...positionDefaults() },
    ]);
    saveDraft(id, state);
    return state.positions.length;
  }

  return { currentId, setCurrent, draft, saveDraft, dropDraft, newId,
           rates, positionDefaults, addPosition, empty };
})();
