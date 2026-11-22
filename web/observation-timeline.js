// Every position is a whole UTC calendar day, including on a short zoomed-in scale.
const dayMs = 86400000;
const day = iso => Date.parse(iso) / dayMs;
const iso = number => new Date(number * dayMs).toISOString().slice(0, 10);
const label = number => iso(number).split('-').reverse().join('.');
const clamp = (n, min, max) => Math.max(min, Math.min(max, n));

export function mountTimeline(panel, onEdit) {
  const find = selector => panel.querySelector(selector);
  const start = find('#range-start');
  const end = find('#range-end');
  const track = find('.nav-track');
  const bands = find('.date-range-bands');
  const ticks = find('.timeline-ticks');
  const help = find('.timeline-help');
  const instructions = help.textContent;
  const scope = find('[data-zoom]');
  const picker = find('[data-interval]');
  const prev = find('[data-prev]');
  const next = find('[data-next]');
  let first, last, left, right, active = 0, periods = [], pending = false, selecting = false;
  let dragStart, dragEnd, dragPointer;
  const value = () => periods[active];
  function span() {
    if (scope.value === 'archive') return last - first;
    const length = value().end - value().start;
    return Math.min(last - first, scope.value === 'auto' ? (length <= 200 ? 364 : Math.max(730, length * 2)) : Number(scope.value) - 1);
  }
  function windowAt(center) {
    const size = span();
    left = clamp(Math.round(center - size / 2), first, last - size);
    right = left + size;
  }
  function centerOnInterval() {
    const {start, end} = value();
    windowAt(end - start <= span() ? (start + end) / 2 : start);
  }
  function render() {
    if (!periods.length) return;
    const size = right - left;
    const percent = n => 100 * (n - left) / (size || 1);
    const selected = value();
    const rows = periods.map((p, i) => {
      if (p.start > right || p.end < left) return null;
      const band = document.createElement('span');
      band.className = 'date-range-band';
      band.dataset.active = String(i === active);
      band.style.left = percent(Math.max(p.start, left)) + '%';
      band.style.right = (100 - percent(Math.min(p.end, right))) + '%';
      return band;
    }).filter(Boolean);
    bands.replaceChildren(...rows);
    for (const [input, date] of [[start, selected.start], [end, selected.end]]) {
      input.max = size;
      input.value = clamp(date - left, 0, size);
      input.hidden = date < left || date > right || pending;
      input.setAttribute('aria-valuetext', label(date));
    }
    prev.disabled = left <= first;
    next.disabled = right >= last;
    find('[data-selection]').textContent = label(selected.start) + ' — ' + label(selected.end);
    const marks = [left, left + Math.round(size / 2), right];
    ticks.replaceChildren(...[...new Set(marks)].map(date => {
      const mark = document.createElement('span');
      mark.textContent = label(date);
      return mark;
    }));
  }
  function edit(a, b, final) {
    const before = periods[active - 1], after = periods[active + 1];
    const min = before ? before.end + 1 : first;
    const max = after ? after.start - 1 : last;
    const newStart = clamp(a, min, max), newEnd = clamp(b, min, max);
    selecting = true;
    try {
      onEdit(active, iso(newStart), iso(newEnd), final);
      if (!final) { periods[active] = {start:newStart, end:newEnd}; render(); }
    } finally { selecting = false; }
  }
  for (const handle of [start, end]) {
    const move = final => {
      if (pending || !periods.length) return;
      const {start:a,end:b} = value();
      const selectedDay = left + handle.valueAsNumber;
      edit(handle === start ? Math.min(selectedDay,b) : a, handle === end ? Math.max(selectedDay,a) : b, final);
    };
    handle.addEventListener('input', () => move(false));
    handle.addEventListener('change', () => move(true));
  }
  scope.addEventListener('change', () => { centerOnInterval(); render(); });
  picker.addEventListener('change', () => { active = Number(picker.value); centerOnInterval(); render(); });
  for (const [button, step] of [[prev,-1],[next,1]]) button.addEventListener('click', () => {
    windowAt((left + right) / 2 + step * Math.max(1, Math.round((right - left + 1) * .75))); render();
  });
  for (const edge of ['start','end']) find('[data-jump="' + edge + '"]').addEventListener('click', () => {
    windowAt(value()[edge]); render();
    (edge === 'start' ? start : end).focus();
  });
  // Dragging on empty space defines a fresh selection for the active interval.
  const at = event => {
    const rect = track.getBoundingClientRect();
    return left + Math.round(clamp((event.clientX - rect.left) / rect.width, 0, 1) * (right - left));
  };
  const preview = find('.timeline-preview');
  function clearDrag() { dragPointer = undefined; preview.hidden = true; }
  track.addEventListener('pointerdown', event => {
    if (event.target !== track || event.button !== 0 || pending || !periods.length) return;
    dragPointer = event.pointerId; dragStart = dragEnd = at(event);
    track.setPointerCapture(dragPointer);
  });
  track.addEventListener('pointermove', event => {
    if (event.pointerId !== dragPointer) return;
    dragEnd = at(event);
    preview.hidden = false;
    const size = right - left || 1;
    preview.style.left = 100 * (Math.min(dragStart, dragEnd) - left) / size + '%';
    preview.style.right = 100 * (right - Math.max(dragStart, dragEnd)) / size + '%';
    find('[data-selection]').textContent = label(Math.min(dragStart, dragEnd)) + ' — ' + label(Math.max(dragStart, dragEnd));
  });
  track.addEventListener('pointerup', event => {
    if (event.pointerId !== dragPointer) return;
    dragEnd = at(event); clearDrag();
    edit(Math.min(dragStart,dragEnd), Math.max(dragStart,dragEnd), true);
  });
  track.addEventListener('pointercancel', () => { clearDrag(); render(); });
  return {
    update(bounds, intervals, hasDraft) {
      if (!bounds || !intervals.length) return;
      const old = periods[active];
      const oldSpan = old ? span() : null;
      const nextPeriods = intervals.map(p => ({start: day(p.start), end: day(p.end)}));
      const newBounds = first !== day(bounds.start) || last !== day(bounds.end);
      first = day(bounds.start); last = day(bounds.end);
      periods = nextPeriods;
      active = Math.min(active, periods.length - 1);
      pending = hasDraft;
      help.textContent = pending
        ? 'Нажмите «Показать», чтобы применить введённые интервалы и продолжить выбор на шкале.'
        : instructions;
      if (newBounds || !old || (!selecting && (oldSpan !== span() || value().start > right || value().end < left))) centerOnInterval();
      picker.closest('label').hidden = periods.length <= 1;
      const options = periods.map((p, i) => new Option('Интервал ' + (i+1) + ' · ' + label(p.start) + ' — ' + label(p.end), String(i)));
      picker.replaceChildren(...options);
      picker.value = String(active);
      render();
    },
  };
}
