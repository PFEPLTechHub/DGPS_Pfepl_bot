window.MGR = window.MGR || {};

// -------------------- Form Validation --------------------
window.validateForm = function(formId = 'editForm') {
  let hasError = false;
  const form = document.getElementById(formId);
  if (!form) return false;

  const fields = [
    { id: "report_date", errId: "err_report_date", msg: "Date is required" },
    { id: "site_name", errId: "err_site_name", msg: "Site is required" },
    { id: "drone_name", errId: "err_drone_name", msg: "Drone is required" },
    { id: "pilot_name", errId: "err_pilot_name", msg: "Pilot name is required" },
    { id: "copilot_name", errId: "err_copilot_name", msg: "Copilot name is required" },
    { id: "dgps_used", errId: "err_dgps_used", msg: "DGPS used is required" },
    { id: "dgps_operators", errId: "err_dgps_operators", msg: "DGPS operators is required" },
    { id: "grid_numbers", errId: "err_grid_numbers", msg: "Grid numbers are required" },
    { id: "gcp_points", errId: "err_gcp_points", msg: "GCP points are required" },
    { id: "base_height_m", errId: "err_base_height_m", msg: "Base height must be > 0" },
    { id: "remark", errId: "err_remark", msg: "Remark is required" }
  ];

  fields.forEach(field => {
    const input = document.getElementById(field.id);
    const error = document.getElementById(field.errId);
    if (!input || !error) return;
    const value = input.value.trim();
    if (!value || (field.id === "base_height_m" && parseFloat(value) <= 0)) {
      error.textContent = field.msg;
      error.classList.remove("hidden");
      hasError = true;
    } else {
      error.classList.add("hidden");
    }
  });

  const flightTimes = form.querySelectorAll('input[name="flight_time[]"]');
  const flightAreas = form.querySelectorAll('input[name="flight_area[]"]');
  const flightUbxs  = form.querySelectorAll('input[name="flight_ubx[]"]');
  const flightBases = form.querySelectorAll('input[name="flight_base[]"]');

  for (let i = 0; i < flightTimes.length; i++) {
    const time = parseFloat(flightTimes[i].value);
    const area = parseFloat(flightAreas[i].value);
    const ubx  = flightUbxs[i].value.trim();
    const base = flightBases[i].value.trim();
    if (isNaN(time) || time < 1) {
      alert(`Flight ${i+1}: Time must be ≥ 1`);
      hasError = true;
    }
    if (isNaN(area) || area <= 0) {
      alert(`Flight ${i+1}: Area must be > 0`);
      hasError = true;
    }
    if (!ubx) {
      alert(`Flight ${i+1}: UBX is required`);
      hasError = true;
    }
    if (!base) {
      alert(`Flight ${i+1}: Base file is required`);
      hasError = true;
    }
  }

  return !hasError;
};

// -------------------- Track (left table) --------------------
MGR.fetchTrack = async function() {
  const datePicker = document.getElementById('datePicker');
  const date = datePicker?.value || '';
  try {
    const res = await fetch(`/api/track?date=${encodeURIComponent(date)}`);
    if (!res.ok) throw new Error('Failed to load data');
    const data = await res.json();
    const tbody = document.getElementById('trackTableBody');
    if (!tbody) return;
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
    console.error('Error in fetchTrack:', err);
  }
};

// -------------------- Reports (right table) --------------------
MGR.fetchReports = async function() {
  const datePicker = document.getElementById('datePicker');
  const employeeSelect = document.getElementById('employeeSelect');
  if (!datePicker || !employeeSelect) return;
  const date = datePicker.value;
  const employee = employeeSelect.value;
  try {
    const res = await fetch(`/api/reports?date=${encodeURIComponent(date)}&employee=${encodeURIComponent(employee)}`);
    if (!res.ok) throw new Error('Failed to load reports');
    const data = await res.json();
    const tbody = document.getElementById('reportsTableBody');
    if (!tbody) return;
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
    console.error('Error in fetchReports:', err);
  }
};

// -------------------- Report Modals --------------------
MGR.viewReport = async function(reportId) {
  try {
    const res = await fetch(`/report/${reportId}/preview?fragment=1`, {
      headers: { 'X-Requested-With': 'fetch' }
    });
    if (!res.ok) throw new Error('Failed to load report');
    const html = await res.text();

    const modal = document.getElementById('viewModal');
    const modalContent = document.getElementById('viewModalContent');
    if (!modal || !modalContent) return;

    modalContent.innerHTML = html;
    modal.classList.remove('hidden');
  } catch (err) {
    alert('Error loading report: ' + err.message);
    console.error('Error in viewReport:', err);
  }
};


MGR.editReport = async function(reportId) {
  try {
    const res = await fetch(`/report/${reportId}/edit`);
    if (!res.ok) throw new Error('Failed to load edit form');
    const html = await res.text();
    const modalContent = document.getElementById('editModalContent');
    const modal = document.getElementById('editModal');
    if (!modalContent || !modal) return;
    modalContent.innerHTML = html;
    modal.classList.remove('hidden');

    const form = document.querySelector("#editModal form");
    if (form) {
      form.addEventListener("submit", async function(e) {
        e.preventDefault();
        if (!window.validateForm('editForm')) return;
        try {
          const res = await fetch(form.action, {
            method: 'POST',
            body: new FormData(form),
          });
          const data = await res.json();
          if (data.ok) {
            alert(data.message);
            modal.classList.add('hidden');
            MGR.fetchReports();
          } else {
            alert('Error: ' + (data.message || 'Failed to update report'));
          }
        } catch (err) {
          alert('Error updating report: ' + err.message);
          console.error('Error in form submission:', err);
        }
      });
    }
  } catch (err) {
    alert('Error loading edit form: ' + err.message);
    console.error('Error in editReport:', err);
  }
};

MGR.deleteReport = async function(reportId) {
  if (!confirm('Are you sure you want to delete this report?')) return;
  try {
    const res = await fetch(`/report/${reportId}/delete`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      alert(data.message);
      MGR.fetchReports();
    } else {
      alert('Error: ' + (data.message || 'Failed to delete report'));
    }
  } catch (err) {
    alert('Error deleting report: ' + err.message);
    console.error('Error in deleteReport:', err);
  }
};

// -------------------- Flight rows (edit form) --------------------
MGR.addFlightRow = function() {
  const tbody = document.getElementById('flightsTableBody');
  if (!tbody) return;
  const noFlightsRow = document.getElementById('noFlightsRow');
  if (noFlightsRow) noFlightsRow.remove();

  const rowCount = tbody.querySelectorAll('.flight-row').length + 1;
  const newRow = document.createElement('tr');
  newRow.className = 'border-b border-gray-200 flight-row';
  newRow.innerHTML = `
    <td class="p-3">${rowCount} <input type="hidden" name="flight_id[]" value=""></td>
    <td class="p-3"><input type="number" min="1" name="flight_time[]" class="w-full p-2 border rounded-lg"></td>
    <td class="p-3"><input type="number" step="0.001" min="0.001" name="flight_area[]" class="w-full p-2 border rounded-lg"></td>
    <td class="p-3"><input type="text" name="flight_ubx[]" class="w-full p-2 border rounded-lg"></td>
    <td class="p-3"><input type="text" name="flight_base[]" class="w-full p-2 border rounded-lg"></td>
    <td class="p-3"><button type="button" onclick="MGR.deleteFlightRow(this)" class="py-1 px-2 bg-red-600 text-white rounded-lg">Delete</button></td>
  `;
  tbody.appendChild(newRow);
};

MGR.deleteFlightRow = function(button) {
  const row = button.closest('tr');
  row.remove();
  const tbody = document.getElementById('flightsTableBody');
  const rows = tbody.querySelectorAll('.flight-row');
  if (rows.length === 0) {
    tbody.innerHTML = '<tr id="noFlightsRow"><td colspan="6" class="p-3 text-center text-gray-600">No flights recorded.</td></tr>';
  } else {
    rows.forEach((row, index) => {
      row.querySelector('td:first-child').firstChild.textContent = index + 1;
    });
  }
};

// -------------------- Page Bootstrap (dashboard) --------------------
document.addEventListener('DOMContentLoaded', () => {
  const btnRefresh = document.getElementById('btnRefresh');
  const datePicker = document.getElementById('datePicker');
  const btnFilter  = document.getElementById('btnFilter');

  if (btnRefresh) btnRefresh.addEventListener('click', MGR.fetchTrack);
  if (datePicker && btnRefresh == null) datePicker.addEventListener('change', MGR.fetchReports);
  if (btnFilter) {
    btnFilter.addEventListener('click', (e) => {
      e.preventDefault();
      MGR.fetchReports();
    });
  }

  if (document.getElementById('reportsTableBody')) MGR.fetchReports();
  if (document.getElementById('trackTableBody')) MGR.fetchTrack();
});

// -------------------- View Reports (Tabbed) --------------------
// One single, modern implementation (removes older/duplicate blocks)
(function () {
  const datePanel = document.getElementById('vrTab-date');
  if (!datePanel) return; // not on View Reports page

  const TABS_KEY = 'viewReports.activeTab';
  const getActiveTab = () => sessionStorage.getItem(TABS_KEY) || 'date';
  const setActiveTab = (t) => sessionStorage.setItem(TABS_KEY, t);

  function activateTab(tab) {
    document.querySelectorAll('.vr-tab-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    document.querySelectorAll('.vr-panel').forEach(p => {
      p.classList.toggle('hidden', p.id !== `vrTab-${tab}`);
    });
    setActiveTab(tab);
  }

  document.querySelectorAll('.vr-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => activateTab(btn.dataset.tab));
  });

  const modeSel    = document.getElementById('vrDateMode');
  const singleWrap = document.getElementById('vrDateSingleWrap');
  const rangeWrap  = document.getElementById('vrDateRangeWrap');
  const singleDate = document.getElementById('vrDateSingle');
  const fromDate   = document.getElementById('vrDateFrom');
  const toDate     = document.getElementById('vrDateTo');

  if (modeSel) {
    modeSel.addEventListener('change', () => {
      const mode = modeSel.value;
      if (mode === 'range') {
        singleWrap.classList.add('hidden');
        rangeWrap.classList.remove('hidden');
      } else {
        rangeWrap.classList.add('hidden');
        singleWrap.classList.remove('hidden');
      }
    });
  }

  async function fetchJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error('Network error');
    return res.json();
  }

  async function loadDateTab() {
    const mode = (modeSel?.value || 'single').toLowerCase();
    let url = '/api/view/date?mode=' + encodeURIComponent(mode);
    if (mode === 'range') {
      const f = fromDate?.value || '';
      const t = toDate?.value || '';
      if (!f || !t) {
        alert('Please select From and To dates.');
        return;
      }
      url += `&from=${encodeURIComponent(f)}&to=${encodeURIComponent(t)}`;
    } else {
      const d = singleDate?.value || '';
      if (d) url += `&date=${encodeURIComponent(d)}`;
    }

    try {
      const data = await fetchJSON(url);
      const tbody = document.getElementById('vrDateTbody');
      tbody.innerHTML = '';
      if (data.message && (data.rows || []).length === 0) {
        alert(data.message);
      }
      (data.rows || []).forEach(r => {
        const tr = document.createElement('tr');
        tr.className = 'border-b border-gray-200';
        tr.innerHTML = `
          <td class="p-3">${r.sr}</td>
          <td class="p-3">${r.first_name || '-'}</td>
          <td class="p-3">${r.last_name || '-'}</td>
          <td class="p-3">
            <a class="text-blue-600 hover:underline cursor-pointer" onclick="MGR.viewReport(${r.id})">View</a>
          </td>
        `;
        tbody.appendChild(tr);
      });
    } catch (e) {
      alert('Failed to load data.');
      console.error(e);
    }
  }

  async function loadEmployeeTab() {
    const empSel = document.getElementById('vrEmpSelect');
    const tg = empSel?.value || '';
    if (!tg) {
      alert('Please select an employee.');
      return;
    }
    const url = `/api/view/employee?employee=${encodeURIComponent(tg)}`;

    try {
      const data = await fetchJSON(url);
      const tbody = document.getElementById('vrEmpTbody');
      tbody.innerHTML = '';
      (data.rows || []).forEach(r => {
        const tr = document.createElement('tr');
        tr.className = 'border-b border-gray-200';
        tr.innerHTML = `
          <td class="p-3">${r.sr}</td>
          <td class="p-3">${r.date}</td>
          <td class="p-3">${r.site_name}</td>
          <td class="p-3">${r.created_at}</td>
          <td class="p-3">
            <a class="text-blue-600 hover:underline cursor-pointer" onclick="MGR.viewReport(${r.id})">View</a>
          </td>
        `;
        tbody.appendChild(tr);
      });
    } catch (e) {
      alert('Failed to load data.');
      console.error(e);
    }
  }

  async function loadSitesTab() {
    const siteSel = document.getElementById('vrSiteSelect');
    const site = siteSel?.value || '';
    const d = (document.getElementById('vrSiteDate')?.value || '');
    if (!site) {
      alert('Please select a site.');
      return;
    }
    let url = `/api/view/sites?site=${encodeURIComponent(site)}`;
    if (d) url += `&date=${encodeURIComponent(d)}`;

    try {
      const data = await fetchJSON(url);
      const tbody = document.getElementById('vrSiteTbody');
      const totalEl = document.getElementById('vrSiteTotalArea');
      tbody.innerHTML = '';
      (data.rows || []).forEach(r => {
        const tr = document.createElement('tr');
        tr.className = 'border-b border-gray-200';
        tr.innerHTML = `
          <td class="p-3">${r.sr}</td>
          <td class="p-3">${r.first_name || '-'}</td>
          <td class="p-3">${r.last_name || '-'}</td>
          <td class="p-3">${r.date}</td>
          <td class="p-3">
            <a class="text-blue-600 hover:underline cursor-pointer" onclick="MGR.viewReport(${r.id})">View</a>
          </td>
        `;
        tbody.appendChild(tr);
      });
      totalEl.textContent = data.total_area || '0.000';
    } catch (e) {
      alert('Failed to load data.');
      console.error(e);
    }
  }

  async function loadDronesTab() {
    const drSel = document.getElementById('vrDroneSelect');
    const dr = drSel?.value || '';
    const d = (document.getElementById('vrDroneDate')?.value || '');
    if (!dr) {
      alert('Please select a drone.');
      return;
    }
    let url = `/api/view/drones?drone=${encodeURIComponent(dr)}`;
    if (d) url += `&date=${encodeURIComponent(d)}`;

    try {
      const data = await fetchJSON(url);
      const tbody = document.getElementById('vrDroneTbody');
      const totalEl = document.getElementById('vrDroneTotalFlights');
      tbody.innerHTML = '';
      (data.rows || []).forEach(r => {
        const tr = document.createElement('tr');
        tr.className = 'border-b border-gray-200';
        tr.innerHTML = `
          <td class="p-3">${r.sr}</td>
          <td class="p-3">${r.first_name || '-'}</td>
          <td class="p-3">${r.last_name || '-'}</td>
          <td class="p-3">${r.date}</td>
          <td class="p-3">
            <a class="text-blue-600 hover:underline cursor-pointer" onclick="MGR.viewReport(${r.id})">View</a>
          </td>
        `;
        tbody.appendChild(tr);
      });
      totalEl.textContent = data.total_flights ?? 0;
    } catch (e) {
      alert('Failed to load data.');
      console.error(e);
    }
  }

  // Apply buttons
  document.getElementById('vrDateFilter')?.addEventListener('click', (e) => {
    e.preventDefault();
    loadDateTab();
  });
  document.getElementById('vrEmpFilter')?.addEventListener('click', (e) => {
    e.preventDefault();
    loadEmployeeTab();
  });
  document.getElementById('vrSiteFilter')?.addEventListener('click', (e) => {
    e.preventDefault();
    loadSitesTab();
  });
  document.getElementById('vrDroneFilter')?.addEventListener('click', (e) => {
    e.preventDefault();
    loadDronesTab();
  });

  // Refresh — reload current tab without resetting inputs
  document.getElementById('vrRefreshBtn')?.addEventListener('click', (e) => {
    e.preventDefault();
    const tab = getActiveTab();
    if (tab === 'date') return loadDateTab();
    if (tab === 'employee') return loadEmployeeTab();
    if (tab === 'sites') return loadSitesTab();
    if (tab === 'drones') return loadDronesTab();
  });

  // Init
  const initial = getActiveTab();
  activateTab(initial);
  // Don’t auto-load Date data; wait for Apply (per your requirement)
})();
