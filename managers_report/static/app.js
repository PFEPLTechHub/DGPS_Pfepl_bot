window.MGR = window.MGR || {};

MGR.fetchTrack = async function() {
  const datePicker = document.getElementById('datePicker');
  const date = datePicker.value;
  try {
    const res = await fetch(`/api/track?date=${encodeURIComponent(date)}`);
    if (!res.ok) throw new Error('Failed to load data');
    const data = await res.json();
    const tbody = document.getElementById('trackTableBody');
    tbody.innerHTML = '';
    (data.rows || []).forEach(row => {
      const tr = document.createElement('tr');
      tr.className = 'border-b border-gray-200';
      tr.innerHTML = `
        <td class="p-3">${row.sr}</td>
        <td class="p-3">${row.name}</td>
        <td class="p-3">${row.time}</td>
        <td class="p-3">
          <span class="${row.status === 'Submitted' ? 'text-green-600' : 'text-red-600'}">
            ${row.status}
          </span>
        </td>
        <td class="p-3">
          ${row.report_id
            ? `<a class="text-blue-600 hover:underline" href="/report/${row.report_id}">View</a> | 
               <a class="text-blue-600 hover:underline" href="/report/${row.report_id}/edit">Edit</a>`
            : '—'
          }
        </td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    alert('Error loading data: ' + err.message);
  }
};

document.addEventListener('DOMContentLoaded', () => {
  const btnRefresh = document.getElementById('btnRefresh');
  const datePicker = document.getElementById('datePicker');
  if (btnRefresh) btnRefresh.addEventListener('click', MGR.fetchTrack);
  if (datePicker) datePicker.addEventListener('change', MGR.fetchTrack);
  MGR.fetchTrack();
});