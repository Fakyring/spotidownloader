const form = document.querySelector('#search-form');
const searchButton = document.querySelector('#search-button');
const searchStatus = document.querySelector('#search-status');
const notes = document.querySelector('#search-notes');
const tracks = document.querySelector('#tracks');
const empty = document.querySelector('#empty');
const toggleAll = document.querySelector('#toggle-all');
const selection = document.querySelector('#selection');
const format = document.querySelector('#format');
const downloadButton = document.querySelector('#download-button');
const downloadProgress = document.querySelector('#download-progress');
const downloadStatus = document.querySelector('#download-status');
const meter = document.querySelector('#meter');
const stage = document.querySelector('#stage');
const percent = document.querySelector('#percent');

let busy = false;
const choices = () => Array.from(tracks.querySelectorAll('input[value]'));

function message(element, text, error = false) {
  element.textContent = text;
  element.classList.toggle('error', error);
  element.hidden = !text;
}

function updateControls() {
  const items = choices();
  const count = items.filter((input) => input.checked).length;
  for (const input of items) input.disabled = busy;
  searchButton.disabled = busy;
  toggleAll.hidden = items.length === 0;
  toggleAll.disabled = busy;
  toggleAll.textContent = count === items.length ? 'Снять выбор' : 'Выбрать все';
  selection.textContent = count ? `Выбрано: ${count} из ${items.length}` : 'Ничего не выбрано';
  format.textContent = count > 1 ? 'Формат загрузки: ZIP' : count === 1 ? 'Формат загрузки: MP3' : 'Один трек: MP3. Несколько: ZIP.';
  downloadButton.disabled = busy || count === 0;
}

function renderTracks(rows) {
  tracks.replaceChildren();
  for (const row of rows) {
    const li = document.createElement('li');
    const label = document.createElement('label');
    label.className = 'track';
    const input = document.createElement('input');
    input.type = 'checkbox';
    if (row.token) input.value = row.token;
    else input.disabled = true;
    const details = document.createElement('span');
    const title = document.createElement('strong');
    const artist = document.createElement('small');
    title.textContent = row.title;
    artist.textContent = row.token ? row.artist : `${row.artist} / превышен лимит 15 мин`;
    details.append(title, artist);
    const length = document.createElement('span');
    length.className = 'length';
    if (row.duration) {
      const seconds = Math.round(row.duration);
      length.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    }
    label.append(input, details, length);
    li.append(label);
    tracks.append(li);
  }
  tracks.hidden = rows.length === 0;
  empty.hidden = rows.length !== 0;
  if (rows.length === 1 && rows[0].token) choices()[0].checked = true;
}

tracks.addEventListener('change', updateControls);
toggleAll.addEventListener('click', () => {
  const items = choices();
  const checked = !items.every((input) => input.checked);
  for (const input of items) input.checked = checked;
  updateControls();
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  updateControls();
  message(searchStatus, 'Ищем треки…');
  message(notes, '');
  message(downloadStatus, '');
  downloadProgress.hidden = true;
  renderTracks([]);
  try {
    const response = await fetch('/search', { method: 'POST', body: new FormData(form) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    renderTracks(data.tracks);
    message(searchStatus, `Найдено треков: ${data.tracks.length}.`);
    message(notes, data.notes.join(' '));
  } catch (error) {
    message(searchStatus, error instanceof SyntaxError ? 'Не удалось получить результаты поиска.' : error.message, true);
  } finally {
    busy = false;
    updateControls();
  }
});

function updateProgress(value, text) {
  meter.value = value;
  percent.textContent = `${value}%`;
  stage.textContent = text;
}

downloadButton.addEventListener('click', async () => {
  const selected = choices().filter((input) => input.checked).map((input) => input.value);
  if (!selected.length || busy) return;
  const job = Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, '0')).join('');
  busy = true;
  updateControls();
  message(downloadStatus, '');
  updateProgress(0, 'Подготовка');
  downloadProgress.hidden = false;

  let preparing = true;
  let polling = false;
  const timer = setInterval(async () => {
    if (!preparing || polling) return;
    polling = true;
    try {
      const response = await fetch(`/progress/${job}`);
      const state = await response.json();
      if (preparing && state.stage) {
        updateProgress(state.percent, `${state.current}/${state.total} · ${state.stage}: ${state.title}`);
      }
    } catch {
      // A missed poll does not interrupt the download.
    } finally {
      polling = false;
    }
  }, 350);

  try {
    const response = await fetch(`/download/${job}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tracks: selected }),
    });
    preparing = false;
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error);
    }
    const transferStage = 'Передача файла в браузер';
    updateProgress(96, transferStage);
    const size = Number(response.headers.get('Content-Length'));
    let received = 0;
    const body = size && response.body ? response.body.pipeThrough(new TransformStream({
      transform(chunk, controller) {
        received += chunk.byteLength;
        updateProgress(Math.min(99, 96 + Math.floor(4 * received / size)), transferStage);
        controller.enqueue(chunk);
      },
    })) : null;
    const blob = body ? await new Response(body, { headers: response.headers }).blob() : await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const plain = disposition.match(/filename="([^"]+)"|filename=([^;]+)/i);
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = (encoded && decodeURIComponent(encoded[1])) || plain?.[1] || plain?.[2] || (selected.length > 1 ? 'Granite.zip' : 'Granite.mp3');
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    updateProgress(100, 'Файл передан браузеру');
    const failed = Number(response.headers.get('X-Failed-Tracks'));
    message(downloadStatus, failed ? `Не удалось скачать треков: ${failed}. Причины указаны в not-downloaded.txt внутри ZIP.` : 'Файл готов и отправлен на скачивание.');
  } catch (error) {
    stage.textContent = 'Скачивание не завершено';
    message(downloadStatus, error instanceof SyntaxError ? 'Не удалось получить файл.' : error.message, true);
  } finally {
    preparing = false;
    clearInterval(timer);
    busy = false;
    updateControls();
  }
});
