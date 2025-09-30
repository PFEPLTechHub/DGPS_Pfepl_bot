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
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    alert('Error loading data: ' + err.message);
  }
};

MGR.fetchReports = async function() {
  const datePicker = document.getElementById('datePicker');
  const employeeSelect = document.getElementById('employeeSelect');
  const date = datePicker.value;
  const employee = employeeSelect.value;
  try {
    const res = await fetch(`/api/reports?date=${encodeURIComponent(date)}&employee=${encodeURIComponent(employee)}`);
    if (!res.ok) throw new Error('Failed to load reports');
    const data = await res.json();
    const tbody = document.getElementById('reportsTableBody');
    tbody.innerHTML = '';
    (data.reports || []).forEach(report => {
      const tr = document.createElement('tr');
      tr.className = 'border-b border-gray-200';
      tr.innerHTML = `
        <td class="p-3">${report.id}</td>
        <td class="p-3">${report.report_date}</td>
        <td class="p-3">${report.site_name}</td>
        <td class="p-3">${report.drone_name}</td>
        <td class="p-3">${report.created_at}</td>
        <td class="p-3">
          <a class="text-blue-600 hover:underline cursor-pointer" onclick="MGR.viewReport(${report.id})">View</a> |
          <a class="text-blue-600 hover:underline" href="/report/${report.id}/edit">Edit</a>
        </td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    alert('Error loading reports: ' + err.message);
  }
};

MGR.viewReport = async function(reportId) {
  try {
    const res = await fetch(`/report/${reportId}/preview`);
    if (!res.ok) throw new Error('Failed to load report');
    const html = await res.text();
    document.getElementById('modalContent').innerHTML = html;
    document.getElementById('viewModal').classList.remove('hidden');
  } catch (err) {
    alert('Error loading report: ' + err.message);
  }
};

document.addEventListener('DOMContentLoaded', () => {
  const btnRefresh = document.getElementById('btnRefresh');
  const datePicker = document.getElementById('datePicker');
  const btnFilter = document.getElementById('btnFilter');
  if (btnRefresh) btnRefresh.addEventListener('click', MGR.fetchTrack);
  if (datePicker && btnRefresh == null) datePicker.addEventListener('change', MGR.fetchReports);
  if (btnFilter) btnFilter.addEventListener('click', (e) => {
    e.preventDefault();
    MGR.fetchReports();
  });
  if (document.getElementById('reportsTableBody')) MGR.fetchReports();
  if (document.getElementById('trackTableBody')) MGR.fetchTrack();
});