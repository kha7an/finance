'use strict';

let sessionToken = '';
const $ = id => document.getElementById(id);
const state = {page: 0, data: null, controller: null, purchaseController: null, purchaseData: null, purchaseBudget: '', editing: false, saving: false, tab: 'overview', filters: null};
const moneyFormat = new Intl.NumberFormat('ru-RU', {style: 'currency', currency: 'RUB', maximumFractionDigits: 2});
const rub = value => moneyFormat.format(Number(value || 0));
const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
const svgNS = 'http://www.w3.org/2000/svg';
function icon(name, cls = '') {
  const node = document.createElementNS(svgNS, 'svg');
  node.setAttribute('class', 'icon ' + cls);
  node.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(svgNS, 'use');
  use.setAttribute('href', '#i-' + name);
  node.append(use);
  return node;
}
function dateValue(value) { return new Date(value.length === 7 ? value + '-01T12:00:00' : value + 'T12:00:00'); }
function dateLabel(value, options = {day: 'numeric', month: 'long'}) {
  return dateValue(value).toLocaleDateString('ru-RU', options);
}
function periodLabel(start, end) {
  const a = dateValue(start), b = dateValue(end);
  const year = a.getFullYear() === b.getFullYear() ? undefined : 'numeric';
  if (start === end) return dateLabel(start, {day: 'numeric', month: 'long', year: 'numeric'});
  if (start.slice(0, 7) === end.slice(0, 7)) return a.getDate() + '–' + dateLabel(end, {day: 'numeric', month: 'long', year: 'numeric'});
  return dateLabel(start, {day: 'numeric', month: 'short', year}) + ' — ' + dateLabel(end, {day: 'numeric', month: 'short', year: 'numeric'});
}
function reportError(error, retry = false) {
  $('status').textContent = error.message;
  $('status-wrap').hidden = false;
  $('retry').hidden = !retry;
}
function showLogin() {
  state.controller?.abort();
  state.purchaseController?.abort();
  sessionToken = '';
  state.data = null;
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
  $('login').hidden = false;
  $('workspace').hidden = true;
  $('account').hidden = true;
}
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (sessionToken) headers.set('Authorization', 'Bearer ' + sessionToken);
  const response = await fetch(path, {credentials: 'same-origin', ...options, headers});
  if (!response.ok) {
    let message = 'Не удалось загрузить данные. Проверьте соединение и попробуйте ещё раз.';
    try { message = (await response.json()).detail || message; } catch {}
    if (response.status === 401) showLogin();
    throw Object.assign(new Error(typeof message === 'string' ? message : 'Проверьте выбранные параметры.'), {status: response.status});
  }
  return response.json();
}
function metric(label, value, sub, iconName, positive = false) {
  const node = el('div', undefined, 'metric' + (positive ? ' positive' : ''));
  const mark = el('span', undefined, 'metric-icon');
  mark.append(icon(iconName));
  node.append(mark, el('div', label, 'label'), el('div', rub(value), 'value'), el('div', sub, 'sub'));
  return node;
}
function hero(label, value, sub, note = '') {
  const node = el('div', undefined, 'hero-card');
  const top = el('div', undefined, 'hero-top');
  top.append(el('span', label, 'eyebrow'), icon('wallet'));
  node.append(top, el('div', rub(value), 'hero-value'), el('p', sub));
  if (note) node.append(el('p', note, 'hero-note'));
  return node;
}
function detailMetric(label, value) {
  const node = el('dl', undefined, 'detail-metric');
  node.append(el('dt', label), el('dd', rub(value)));
  return node;
}
function dot(name, colors) {
  const node = el('i', undefined, 'dot');
  node.style.setProperty('--dot', colors[name] || '#8795a8');
  return node;
}
function empty(title, description, iconName = 'receipt', clear = false) {
  const node = el('div', undefined, 'empty'), mark = el('span', undefined, 'section-icon');
  mark.append(icon(iconName));
  node.append(mark, el('strong', title), el('p', description));
  if (clear) {
    const button = el('button', 'Сбросить фильтры', 'text-button');
    button.onclick = clearFilters;
    node.append(button);
  }
  return node;
}
function selectOptions(id, rows, label, value) {
  const node = $(id), selected = node.value;
  while (node.options.length > 1) node.remove(1);
  for (const row of rows) {
    const option = el('option', label(row));
    option.value = value(row);
    node.append(option);
  }
  node.value = selected;
  if (node.selectedIndex < 0) node.selectedIndex = 0;
}
function readFilters() {
  return Object.fromEntries(['start', 'end', 'category', 'author', 'search', 'mandatory'].map(id => [id, id === 'mandatory' ? $(id).checked : $(id).value]));
}
function writeFilters(values) {
  for (const [id, value] of Object.entries(values)) {
    if (id === 'mandatory') $(id).checked = value;
    else $(id).value = value;
  }
}
function updateFilterSummary() {
  const f = state.filters, labels = [];
  if (f.category) labels.push(f.category);
  if (f.search) labels.push('«' + f.search + '»');
  if (f.author) labels.push('Автор: ' + f.author.replace(/^telegram:/, ''));
  if (f.mandatory) labels.push('Без обязательных');
  $('filter-count').textContent = labels.length;
  $('filter-count').hidden = !labels.length;
  $('active-filters').hidden = !labels.length || state.tab === 'planning';
  $('filter-summary').textContent = labels.join(' · ');
}
async function load() {
  state.controller?.abort();
  const controller = new AbortController();
  state.controller = controller;
  $('status-wrap').hidden = true;
  $('loading').hidden = false;
  $('content').setAttribute('aria-busy', 'true');
  $('refresh').disabled = true;
  const filters = state.filters || readFilters();
  const params = new URLSearchParams({budget: $('budget').value, page: state.page, sort: $('sort').value, bucket: $('bucket').value, exclude_mandatory: filters.mandatory});
  for (const key of ['start', 'end', 'category', 'search', 'author']) if (filters[key]) params.set(key, filters[key]);
  try {
    const data = await api('/api/dashboard?' + params, {signal: controller.signal});
    state.data = data;
    state.filters = {...filters, start: data.start, end: data.end};
    render(data);
  } catch (error) {
    if (error.name !== 'AbortError') reportError(error, !$('workspace').hidden);
  } finally {
    if (state.controller === controller) {
      $('content').setAttribute('aria-busy', 'false');
      $('loading').hidden = true;
      $('refresh').disabled = false;
    }
  }
}
function transaction(row, colors) {
  const node = el('button', undefined, 'transaction');
  node.type = 'button';
  node.setAttribute('aria-label', row.name + ', ' + rub(row.personal_amount) + ', ' + dateLabel(row.operation_date) + '. Детали покупки');
  const mark = el('span', undefined, 'transaction-icon');
  mark.style.setProperty('--category', colors[row.category] || '#8795a8');
  mark.append(icon('receipt'));
  const main = el('span', undefined, 'transaction-main');
  const category = el('span', undefined, 'transaction-category');
  category.append(dot(row.category, colors), document.createTextNode(' ' + [row.category, row.subcategory].filter(Boolean).join(' / ')));
  main.append(el('span', row.name, 'transaction-name'), category);
  const values = el('span', undefined, 'transaction-values');
  values.append(el('span', rub(row.personal_amount), 'transaction-amount'));
  if (Number(row.returned_amount) > 0) values.append(el('span', 'Возврат ' + rub(row.returned_amount), 'transaction-return'));
  node.append(mark, main, values, icon('chevron', 'chevron'));
  node.onclick = () => purchase(row.id);
  return node;
}
function renderPurchases(data) {
  const filtered = !!(state.filters.category || state.filters.search || state.filters.author || state.filters.mandatory);
  $('recent').replaceChildren();
  $('entries').replaceChildren();
  const recent = [...data.expenses].sort((a, b) => b.operation_date.localeCompare(a.operation_date) || b.id - a.id).slice(0, 4);
  for (const row of recent) {
    const node = transaction(row, data.colors);
    const category = node.querySelector('.transaction-category');
    category.replaceChildren(dot(row.category, data.colors), document.createTextNode(' ' + row.category + ' · ' + dateLabel(row.operation_date, {day: 'numeric', month: 'short'})));
    $('recent').append(node);
  }
  const chronological = $('sort').value.startsWith('date');
  let previousDate = '';
  for (const row of data.expenses) {
    if (chronological && row.operation_date !== previousDate) {
      const heading = el('div', undefined, 'date-heading');
      heading.append(el('span', dateLabel(row.operation_date, {day: 'numeric', month: 'long', year: 'numeric'})));
      $('entries').append(heading);
      previousDate = row.operation_date;
    }
    const node = transaction(row, data.colors);
    if (!chronological) node.querySelector('.transaction-category').append(document.createTextNode(' · ' + dateLabel(row.operation_date)));
    $('entries').append(node);
  }
  if (!data.expenses.length) {
    for (const id of ['recent', 'entries']) $(id).append(empty('Пока нет покупок', filtered ? 'Попробуйте другой период или сбросьте фильтры.' : 'Выберите другой период или добавьте расход в чате с ботом.', 'receipt', filtered));
  }
  $('count').textContent = data.count + ' ' + plural(data.count, ['покупка', 'покупки', 'покупок']) + ' за период';
  $('page').textContent = 'Страница ' + (data.page + 1) + ' из ' + Math.max(1, Math.ceil(data.count / 25));
  $('prev').disabled = data.page === 0;
  $('next').disabled = (data.page + 1) * 25 >= data.count;
}
function plural(value, forms) {
  const n = Math.abs(value) % 100, last = n % 10;
  return n > 10 && n < 20 ? forms[2] : last === 1 ? forms[0] : last >= 2 && last <= 4 ? forms[1] : forms[2];
}
function render(data) {
  if (!$('filter-dialog').open) writeFilters(state.filters);
  selectOptions('category', data.categories.filter(row => row.kind === 'expense'), row => row.name, row => row.name);
  selectOptions('author', data.members, row => 'Участник ' + row.user_id.replace(/^telegram:/, ''), row => row.user_id);
  $('period-label').textContent = periodLabel(data.start, data.end);
  updateFilterSummary();
  const m = data.metrics;
  $('metrics').replaceChildren(
    hero('ЛИЧНЫЕ РАСХОДЫ', m.expenses, 'За выбранный период · с учётом возвратов', data.count + ' ' + plural(data.count, ['покупка', 'покупки', 'покупок']) + ' в вашей истории'),
    metric('Доходы', m.income, 'Без возмещений', 'arrow-down', true),
    metric('Движение денег', m.cash_flow, 'Поступления минус оплаты', 'chart')
  );
  $('secondary-metrics').replaceChildren(detailMetric('Исходные оплаты', m.paid), detailMetric('Полученные возмещения', m.reimbursements), detailMetric('Корректировка покупок', m.adjustment));
  $('adjustment').textContent = 'Возвраты учтены на момент загрузки. Исключено обязательных расходов: ' + rub(m.excluded) + '.';
  renderPurchases(data);
  chart(data);
  renderPlanning(data);
}
function renderPlanning(data) {
  const p = data.planning;
  $('daily-preview').textContent = Number(p.available) < 0 ? 'Дефицит ' + rub(-Number(p.available)) : rub(p.daily) + ' / день';
  $('daily-caption').textContent = 'До конца периода — ' + p.days_left + ' ' + plural(p.days_left, ['день', 'дня', 'дней']);
  $('plan-dates').textContent = periodLabel(p.start, p.end);
  $('planmetrics').replaceChildren(
    hero('ДОСТУПНО В ПЕРИОДЕ', p.available, Number(p.available) < 0 ? 'Расходы и обязательства превышают средства' : 'После резерва обязательных платежей', 'По записанным операциям, не баланс счёта'),
    metric('Можно на день', p.daily, 'Осталось дней: ' + p.days_left, 'wallet', true),
    metric('Резерв платежей', p.reserve, 'Ещё не проведены', 'calendar')
  );
  $('planperiod').textContent = 'Начальная сумма: ' + rub(p.opening) + '. Доходы: ' + rub(p.income) + '; исходные оплаты: ' + rub(p.paid) + '; возмещения: ' + rub(p.reimbursements) + '; личные расходы: ' + rub(p.all_expenses) + '.';
  $('payments').replaceChildren();
  const today = new Intl.DateTimeFormat('sv-SE', {timeZone: 'Europe/Moscow'}).format(new Date());
  for (const row of p.upcoming) {
    const overdue = row.due_date < today, item = el('div', undefined, 'payment' + (overdue ? ' overdue' : ''));
    const description = el('div');
    description.append(el('span', row.name, 'payment-name'), el('small', dateLabel(row.due_date) + ' · ' + (overdue ? 'Просрочен · ' : '') + (row.operation_type === 'expense' ? 'Расход' : 'Доход')));
    item.append(description, el('strong', rub(row.amount)));
    $('payments').append(item);
  }
  if (!p.upcoming.length) $('payments').append(empty('Платежей пока нет', 'Добавьте регулярные платежи в планировании у бота.', 'calendar'));
  $('limits').replaceChildren();
  for (const [name, value] of Object.entries(data.limits)) {
    const item = el('div', undefined, 'payment'), label = el('span', name || 'Общий лимит', 'payment-name');
    item.append(label, el('strong', rub(value)));
    $('limits').append(item);
  }
  if (!Object.keys(data.limits).length) $('limits').append(empty('Без заданных лимитов', 'Установите общий лимит или лимит категории в чате с ботом.', 'wallet'));
}
function chart(data) {
  const host = $('chart'), legend = $('legend');
  host.replaceChildren();
  legend.replaceChildren();
  $('chart-detail').textContent = 'Коснитесь точки на графике, чтобы увидеть сумму.';
  $('chart-detail').hidden = !data.series.length;
  if (!data.series.length) {
    host.append(empty('Здесь будет ваша динамика', 'За выбранный период нет записанных расходов.', 'chart'));
    return;
  }
  const svg = document.createElementNS(svgNS, 'svg');
  const width = host.clientWidth < 500 ? 360 : 900, height = width === 360 ? 250 : 320;
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('role', 'group');
  svg.setAttribute('aria-label', 'Динамика личных расходов по категориям. Точки доступны с клавиатуры.');
  const make = (tag, attrs, text) => {
    const node = document.createElementNS(svgNS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    svg.append(node);
    return node;
  };
  const dates = [...new Set(data.series.map(row => row.date))].sort();
  const names = [...new Set(data.series.map(row => row.category))];
  const max = Math.max(1, ...data.series.map(row => Number(row.amount)));
  const left = width === 360 ? 55 : 75, right = width - 18, bottom = height - 38;
  const x = i => left + (dates.length === 1 ? (right - left) / 2 : i / (dates.length - 1) * (right - left));
  const y = v => bottom - Number(v) / max * (bottom - 20);
  const axisMoney = v => new Intl.NumberFormat('ru-RU', {notation: 'compact', maximumFractionDigits: 1}).format(v) + ' ₽';
  for (let i = 0; i <= 4; i++) {
    const value = max * i / 4;
    make('line', {x1: left, x2: right, y1: y(value), y2: y(value), stroke: '#eeedf4', 'stroke-dasharray': '3 5'});
    make('text', {x: left - 10, y: y(value) + 4, 'text-anchor': 'end', fill: '#8a879b', 'font-size': 10}, axisMoney(value));
  }
  dates.forEach((day, i) => {
    const step = Math.max(1, Math.ceil(dates.length / (width === 360 ? 3 : 6)));
    if (i === 0 || i === dates.length - 1 || (i % step === 0 && i < dates.length - step / 2)) {
      const label = dateLabel(day, day.length === 7 ? {month: 'short', year: '2-digit'} : {day: 'numeric', month: 'short'});
      make('text', {x: x(i), y: height - 12, 'text-anchor': i === dates.length - 1 && dates.length > 1 ? 'end' : 'middle', fill: '#8a879b', 'font-size': 10}, label);
    }
  });
  for (const name of names) {
    const values = new Map(data.series.filter(row => row.category === name).map(row => [row.date, row.amount]));
    const color = data.colors[name] || '#8795a8';
    make('polyline', {points: dates.map((day, i) => `${x(i)},${y(values.get(day) || 0)}`).join(' '), fill: 'none', stroke: color, 'stroke-width': 2.5, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'});
    dates.forEach((day, i) => {
      if (!values.has(day)) return;
      const value = values.get(day), label = dateLabel(day) + ' · ' + name + ' · ' + rub(value);
      const point = make('circle', {cx: x(i), cy: y(value), r: 4, fill: color, tabindex: 0, role: 'button', 'aria-label': label});
      const title = document.createElementNS(svgNS, 'title');
      title.textContent = label;
      point.append(title);
      // A transparent stroke makes small chart points easier to tap.
      point.setAttribute('stroke', 'transparent');
      point.setAttribute('stroke-width', 14);
      point.setAttribute('paint-order', 'stroke');
      point.onclick = point.onfocus = () => { $('chart-detail').textContent = label; };
      point.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); $('chart-detail').textContent = label; } };
    });
    const item = el('span');
    item.append(dot(name, data.colors), document.createTextNode(name));
    legend.append(item);
  }
  host.append(svg);
}
function syncBackButton() {
  const app = window.Telegram?.WebApp;
  if (!app?.initData || !app.isVersionAtLeast('6.1')) return;
  if (!$('workspace').hidden && (document.querySelector('dialog[open]') || state.tab !== 'overview')) app.BackButton.show();
  else app.BackButton.hide();
}
function openSheet(id) {
  $(id).showModal();
  syncBackButton();
}
function closeSheet(id) {
  if (id === 'purchase' && state.saving) return;
  $(id).close();
}
async function purchase(id) {
  if (state.saving) return;
  state.purchaseData = null;
  state.editing = false;
  state.purchaseBudget = $('budget').value;
  $('purchase-edit').hidden = $('purchase-feedback').hidden = true;
  $('purchase-content').hidden = false;
  $('purchase-title').textContent = 'ДЕТАЛИ ПОКУПКИ';
  state.purchaseController?.abort();
  const controller = new AbortController();
  state.purchaseController = controller;
  $('purchase-content').replaceChildren(el('p', 'Загружаем покупку…', 'caption'));
  openSheet('purchase');
  try {
    const data = await api('/api/purchases/' + id + '?budget=' + encodeURIComponent($('budget').value), {signal: controller.signal});
    if (!$('purchase').open) return;
    renderPurchase(data);
  } catch (error) {
    if (error.name !== 'AbortError' && $('purchase').open) {
      const retry = el('button', 'Попробовать ещё раз', 'text-button');
      retry.onclick = () => purchase(id);
      $('purchase-content').replaceChildren(el('p', error.message, 'caption'), retry);
    } else if (error.name !== 'AbortError') reportError(error);
  }
}
function renderPurchase(data) {
  state.purchaseData = data;
  state.editing = false;
  $('purchase-edit').hidden = true;
  $('purchase-content').hidden = false;
  $('purchase-title').textContent = 'ДЕТАЛИ ПОКУПКИ';
  const p = data.purchase, content = $('purchase-content');
  content.replaceChildren(el('h2', p.name), el('p', [dateLabel(p.operation_date, {day: 'numeric', month: 'long', year: 'numeric'}), p.category, p.subcategory].filter(Boolean).join(' · '), 'caption'), hero('ЛИЧНЫЙ РАСХОД', p.personal_amount, 'Оплата за вычетом связанных возвратов'));
  const details = el('div', undefined, 'detail-metrics');
  details.append(detailMetric('Оплачено', p.amount), detailMetric('Возвращено', p.returned_amount));
  content.append(details, el('h3', 'Связанные возвраты'));
  for (const r of data.reimbursements) {
    const row = el('div', undefined, 'payment'), description = el('div');
    description.append(el('span', r.name, 'payment-name'), el('small', dateLabel(r.operation_date) + ' · #' + r.id));
    row.append(description, el('strong', rub(r.amount)));
    content.append(row);
  }
  if (!data.reimbursements.length) content.append(el('p', 'У этой покупки пока нет возвратов.', 'caption'));
  if (data.can_edit) {
    const button = el('button', 'Редактировать', 'secondary-button purchase-edit-button');
    button.id = 'edit-purchase';
    button.onclick = startEditingPurchase;
    content.append(button);
  } else {
    content.append(el('p', 'Редактировать эту запись может её автор или владелец бюджета.', 'caption panel-caption'));
  }
}
function fillEditSubcategories(selected = '') {
  const category = state.purchaseData.categories.find(row => row.name === $('edit-category').value);
  const placeholder = el('option', 'Выберите подкатегорию');
  placeholder.value = '';
  const options = (category?.subcategories || []).map(row => {
    const option = el('option', row.name);
    option.value = row.name;
    return option;
  });
  $('edit-subcategory').replaceChildren(placeholder, ...options);
  $('edit-subcategory').value = selected;
  if ($('edit-subcategory').selectedIndex < 0) $('edit-subcategory').value = '';
  if (!selected && options.length === 1) $('edit-subcategory').value = options[0].value;
}
function startEditingPurchase() {
  const data = state.purchaseData;
  if (!data?.can_edit || state.saving) return;
  const p = data.purchase;
  $('edit-name').value = p.name;
  $('edit-date').value = p.operation_date;
  $('edit-amount').value = String(p.amount).replace('.', ',');
  $('edit-amount').setCustomValidity('');
  $('edit-name').setCustomValidity('');
  const placeholder = el('option', 'Выберите категорию');
  placeholder.value = '';
  $('edit-category').replaceChildren(placeholder, ...data.categories.map(row => {
    const option = el('option', row.name);
    option.value = row.name;
    return option;
  }));
  $('edit-category').value = p.category || '';
  if ($('edit-category').selectedIndex < 0) $('edit-category').value = '';
  fillEditSubcategories(p.subcategory || '');
  $('edit-amount-hint').textContent = Number(p.returned_amount) > 0
    ? 'Уже возвращено ' + rub(p.returned_amount) + '. Оплата не может быть меньше этой суммы.'
    : 'Укажите сумму исходной оплаты. Возвраты учитываются отдельно.';
  $('edit-error').hidden = $('edit-reload').hidden = $('purchase-feedback').hidden = true;
  $('purchase-content').hidden = true;
  $('purchase-edit').hidden = false;
  $('purchase-title').textContent = 'РЕДАКТИРОВАНИЕ ПОКУПКИ';
  state.editing = true;
  $('purchase').scrollTop = 0;
}
function cancelEditingPurchase() {
  if (state.saving) return;
  state.editing = false;
  $('purchase-edit').hidden = true;
  $('purchase-content').hidden = false;
  $('purchase-title').textContent = 'ДЕТАЛИ ПОКУПКИ';
  $('edit-purchase')?.focus({preventScroll: true});
}
async function savePurchase(event) {
  event.preventDefault();
  if (state.saving || !state.purchaseData?.can_edit) return;
  const amount = $('edit-amount').value.replace(/\s/g, '').replace(',', '.');
  if (!/^\d+(\.\d{1,2})?$/.test(amount) || Number(amount) <= 0) {
    $('edit-amount').setCustomValidity('Введите положительную сумму с точностью до копейки.');
    $('edit-amount').reportValidity();
    return;
  }
  const name = $('edit-name').value.trim();
  if (!name) {
    $('edit-name').setCustomValidity('Введите название покупки.');
    $('edit-name').reportValidity();
    return;
  }
  const data = state.purchaseData, budget = state.purchaseBudget;
  state.saving = true;
  $('edit-fields').disabled = $('edit-save').disabled = $('edit-cancel').disabled = true;
  $('purchase').querySelector('[data-close]').disabled = true;
  $('edit-save').textContent = 'Сохраняем…';
  $('edit-error').hidden = $('edit-reload').hidden = true;
  try {
    const updated = await api('/api/purchases/' + data.purchase.id + '?budget=' + encodeURIComponent(budget), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json', 'X-Budget-Edit': '1'},
      body: JSON.stringify({operation_date: $('edit-date').value, amount, name,
        category: $('edit-category').value, subcategory: $('edit-subcategory').value, version: data.version})
    });
    if (!$('purchase').open || $('budget').value !== budget) return;
    renderPurchase(updated);
    $('purchase-feedback').textContent = 'Изменения сохранены';
    $('purchase-feedback').hidden = false;
    $('purchase').scrollTop = 0;
    state.page = 0;
    await load();
  } catch (error) {
    if ($('purchase').open) {
      $('edit-error').textContent = error.message;
      $('edit-error').hidden = false;
      $('edit-reload').hidden = error.status !== 409;
    } else reportError(error);
  } finally {
    state.saving = false;
    $('edit-fields').disabled = $('edit-save').disabled = $('edit-cancel').disabled = false;
    $('purchase').querySelector('[data-close]').disabled = false;
    $('edit-save').textContent = 'Сохранить';
  }
}
const views = {
  overview: ['ВАША ФИНАНСОВАЯ КАРТИНА', 'Всё самое важное'],
  expenses: ['КАЖДАЯ ПОКУПКА НА МЕСТЕ', 'На что уходят деньги'],
  trends: ['ЗАМЕЧАЕМ ЗАКОНОМЕРНОСТИ', 'Бюджет в динамике'],
  planning: ['СЕГОДНЯ И НАПЕРЁД', 'Планы на период']
};
function navigate(tab) {
  const previous = state.tab;
  state.tab = tab;
  document.querySelectorAll('[data-view]').forEach(view => { view.hidden = view.dataset.view !== tab; });
  document.querySelectorAll('[data-tab]').forEach(button => {
    if (button.dataset.tab === tab) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  $('view-eyebrow').textContent = views[tab][0];
  $('view-title').textContent = views[tab][1];
  $('period-toolbar').hidden = tab === 'planning';
  if (state.filters) updateFilterSummary();
  if (tab === 'overview' && previous !== 'overview' && (state.page || $('sort').value !== 'date')) {
    state.page = 0;
    $('sort').value = 'date';
    load();
  }
  if (tab === 'trends' && state.data) chart(state.data);
  syncBackButton();
  if (previous !== tab) {
    window.scrollTo({top: 0, behavior: 'instant'});
    $('view-title').focus({preventScroll: true});
  }
}
function openFilters() {
  if (state.filters) writeFilters(state.filters);
  document.querySelectorAll('[data-period]').forEach(button => button.removeAttribute('aria-pressed'));
  $('start').setCustomValidity('');
  openSheet('filter-dialog');
}
function resetFilterValues() {
  writeFilters({start: '', end: '', category: '', author: '', search: '', mandatory: false});
  $('start').setCustomValidity('');
}
function clearFilters() {
  resetFilterValues();
  state.filters = readFilters();
  state.page = 0;
  closeSheet('filter-dialog');
  load();
}
function localISO(date) {
  return date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0') + '-' + String(date.getDate()).padStart(2, '0');
}
function initTelegram() {
  const app = window.Telegram?.WebApp;
  if (!app?.initData) return;
  app.ready();
  app.expand();
  if (app.isVersionAtLeast('6.1')) {
    app.setBackgroundColor('#f7f7fb');
    app.BackButton.onClick(() => {
      if (state.saving) return;
      if (state.editing) { cancelEditingPurchase(); return; }
      const dialog = document.querySelector('dialog[open]');
      if (dialog) dialog.close();
      else navigate('overview');
    });
  }
  if (app.isVersionAtLeast('6.9')) app.setHeaderColor('#f7f7fb');
  if (app.isVersionAtLeast('7.10')) app.setBottomBarColor('#ffffff');
}
async function init() {
  try {
    initTelegram();
    const token = new URLSearchParams(location.hash.slice(1)).get('login');
    const initData = window.Telegram?.WebApp?.initData;
    if (token || initData) {
      history.replaceState(null, '', location.pathname);
      const login = await api('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(initData ? {init_data: initData} : {token})});
      sessionToken = login.session_token || '';
    }
    const budgets = await api('/api/budgets');
    if (!budgets.length) throw new Error('Нет доступных бюджетов. Откройте личный чат с ботом.');
    $('budget').replaceChildren(...budgets.map(row => {
      const option = el('option', row.name);
      option.value = row.id;
      return option;
    }));
    $('login').hidden = true;
    $('workspace').hidden = false;
    $('account').hidden = false;
    await load();
  } catch (error) {
    showLogin();
    reportError(error);
  }
}

$('filters').onsubmit = event => {
  event.preventDefault();
  if ($('start').value > $('end').value) {
    $('start').setCustomValidity('Начало периода должно быть раньше его окончания.');
    $('start').reportValidity();
    return;
  }
  state.filters = readFilters();
  state.page = 0;
  closeSheet('filter-dialog');
  load();
};
for (const id of ['start', 'end']) $(id).oninput = () => {
  $('start').setCustomValidity('');
  document.querySelectorAll('[data-period]').forEach(button => button.removeAttribute('aria-pressed'));
};
$('budget').onchange = () => {
  state.page = 0;
  state.data = null;
  resetFilterValues();
  state.filters = readFilters();
  // Clear the previous budget's content before requesting another budget.
  for (const id of ['metrics', 'recent', 'entries', 'secondary-metrics', 'planmetrics', 'payments', 'limits', 'chart', 'legend']) $(id).replaceChildren();
  for (const id of ['daily-preview', 'daily-caption', 'planperiod', 'plan-dates', 'adjustment', 'count', 'page', 'chart-detail']) $(id).textContent = '';
  selectOptions('category', [], row => row.name, row => row.name);
  selectOptions('author', [], row => row.user_id, row => row.user_id);
  $('period-label').textContent = 'Период бюджета';
  updateFilterSummary();
  load();
};
for (const id of ['sort', 'bucket']) $(id).onchange = () => { state.page = 0; load(); };
$('prev').onclick = () => { state.page = Math.max(0, state.page - 1); load(); };
$('next').onclick = () => { state.page++; load(); };
$('period-button').onclick = $('filter-button').onclick = openFilters;
$('reset-filters').onclick = $('clear-filters').onclick = clearFilters;
$('refresh').onclick = $('retry').onclick = load;
$('all-purchases').onclick = () => navigate('expenses');
$('open-planning').onclick = () => navigate('planning');
document.querySelectorAll('[data-tab]').forEach(button => { button.onclick = () => navigate(button.dataset.tab); });
document.querySelectorAll('[data-close]').forEach(button => { button.onclick = () => closeSheet(button.dataset.close); });
document.querySelectorAll('dialog').forEach(dialog => {
  dialog.addEventListener('cancel', event => { if (dialog.id === 'purchase' && state.saving) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    if (dialog.id === 'purchase') {
      state.purchaseController?.abort();
      state.purchaseData = null;
      state.editing = false;
    }
    syncBackButton();
  });
  dialog.addEventListener('click', event => {
    if (dialog.id === 'purchase' && state.saving) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close();
  });
});
document.querySelectorAll('[data-period]').forEach(button => {
  button.onclick = () => {
    const today = dateValue(new Intl.DateTimeFormat('sv-SE', {timeZone: 'Europe/Moscow'}).format(new Date()));
    if (button.dataset.period === 'budget') {
      // Empty dates ask the server for the configured financial period.
      $('start').value = '';
      $('end').value = '';
      $('start').required = $('end').required = false;
    } else {
      $('start').required = $('end').required = true;
      const offset = button.dataset.period === 'previous' ? -1 : 0;
      const first = new Date(today.getFullYear(), today.getMonth() + offset, 1, 12);
      const last = offset === -1 ? new Date(today.getFullYear(), today.getMonth(), 0, 12) : today;
      $('start').value = localISO(first);
      $('end').value = localISO(last);
    }
    $('start').setCustomValidity('');
    document.querySelectorAll('[data-period]').forEach(preset => preset.setAttribute('aria-pressed', String(preset === button)));
  };
});
$('logout').onclick = async () => {
  $('logout').disabled = true;
  try {
    await api('/api/logout', {method: 'POST'});
    showLogin();
    $('status-wrap').hidden = true;
    if (window.Telegram?.WebApp?.initData) window.Telegram.WebApp.close();
  } catch (error) { reportError(error); }
  finally { $('logout').disabled = false; }
};
$('purchase-edit').onsubmit = savePurchase;
$('edit-cancel').onclick = cancelEditingPurchase;
$('edit-category').onchange = () => fillEditSubcategories();
$('edit-reload').onclick = () => purchase(state.purchaseData.purchase.id);
for (const id of ['edit-name', 'edit-amount']) $(id).oninput = () => $(id).setCustomValidity('');
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state.tab === 'trends' && state.data) chart(state.data); }, 150);
});
init();
