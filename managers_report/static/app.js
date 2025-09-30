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
          <a class="text-blue-600 hover:underline cursor-pointer" onclick="MGR.editReport(${report.id})">Edit</a> |
          <a class="text-red-600 hover:underline cursor-pointer" onclick="MGR.deleteReport(${report.id})">Delete</a>
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
    document.getElementById('viewModalContent').innerHTML = html;
    document.getElementById('viewModal').classList.remove('hidden');
  } catch (err) {
    alert('Error loading report: ' + err.message);
  }
};

MGR.editReport = async function(reportId) {
  try {
    const res = await fetch(`/report/${reportId}/edit`);
    if (!res.ok) throw new Error('Failed to load edit form');
    const html = await res.text();
    document.getElementById('editModalContent').innerHTML = html;
    document.getElementById('editModal').classList.remove('hidden');
    // Re-attach form validation
    document.querySelector("#editModal form")?.addEventListener("submit", async function(e) {
      e.preventDefault();
      if (!window.validateForm()) return;
      const form = e.target;
      try {
        const res = await fetch(form.action, {
          method: 'POST',
          body: new FormData(form),
        });
        const data = await res.json();
        if (data.ok) {
          alert(data.message);
          document.getElementById('editModal').classList.add('hidden');
          MGR.fetchReports();
        } else {
          alert('Error: ' + (data.message || 'Failed to update report'));
        }
      } catch (err) {
        alert('Error updating report: ' + err.message);
      }
    });
  } catch (err) {
    alert('Error loading edit form: ' + err.message);
  }
};

MGR.deleteReport = async function(reportId) {
  if (!confirm('Are you sure you want to delete this report?')) return;
  try {
    const res = await fetch(`/report/${reportId}/delete`, {
      method: 'POST',
    });
    const data = await res.json();
    if (data.ok) {
      alert(data.message);
      MGR.fetchReports();
    } else {
      alert('Error: ' + (data.message || 'Failed to delete report'));
    }
  } catch (err) {
    alert('Error deleting report: ' + err.message);
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