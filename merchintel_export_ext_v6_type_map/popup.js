const $ = s => document.querySelector(s);
const $all = s => Array.from(document.querySelectorAll(s));
const statusEl = $('#status');

$('#runBtn').addEventListener('click', async () => {
  const baseUrl = $('#baseUrl').value.trim();
  const startPage = parseInt($('#startPage').value, 10);
  const endPage = parseInt($('#endPage').value, 10);
  const infiniteMode = $('#infiniteMode').checked;
  const scrollPasses = parseInt($('#scrollPasses').value, 10);
  const scrollDelay = parseInt($('#scrollDelay').value, 10);
  const selectedTypes = $all('.type:checked').map(x => x.value);

  if (!baseUrl) { statusEl.textContent = 'Nhập Link gốc.'; return; }
  if (!infiniteMode) {
    if (isNaN(startPage) || isNaN(endPage) || startPage < 1 || endPage < startPage) {
      statusEl.textContent = 'Khoảng trang không hợp lệ.'; return;
    }
  }

  statusEl.textContent = 'Đang khởi chạy background...';

  try {
    await chrome.runtime.sendMessage({
      type: 'MI_V6_START',
      payload: { baseUrl, startPage, endPage, infiniteMode, scrollPasses, scrollDelay, selectedTypes }
    });
    statusEl.textContent = 'Đang chạy. Xem log trong Service Worker console (chrome://extensions).';
  } catch (e) {
    console.error(e);
    statusEl.textContent = 'Không gửi được lệnh. Kiểm tra Service Worker.';
  }
});
