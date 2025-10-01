window.MGR = window.MGR || {};

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
  const flightUbxs = form.querySelectorAll('input[name="flight_ubx[]"]');
  const flightBases = form.querySelectorAll('input[name="flight_base[]"]');

  for (let i = 0; i < flightTimes.length; i++) {
    const time = parseFloat(flightTimes[i].value);
    const area = parseFloat(flightAreas[i].value);
    const ubx = flightUbxs[i].value.trim();
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

MGR.fetchTrack = async function() {
  const datePicker = document.getElementById('datePicker');
  const date = datePicker.value;
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

MGR.viewReport = async function(reportId) {
  try {
    const res = await fetch(`/report/${reportId}/preview`);
    if (!res.ok) throw new Error('Failed to load report');
    const html = await res.text();
    const modalContent = document.getElementById('viewModalContent');
    const modal = document.getElementById('viewModal');
    if (!modalContent || !modal) return;
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
// ===== View Reports (Tabbed) =====
MGR.initViewReports = function() {
  const tabs = document.querySelectorAll('.vr-tab');
  const panels = document.querySelectorAll('.vr-tabpanel');
  const refresh = document.getElementById('vrRefresh');

  function activate(tabName) {
    tabs.forEach(t => {
      if (t.dataset.tab === tabName) {
        t.classList.add('bg-blue-50', 'text-blue-700');
      } else {
        t.classList.remove('bg-blue-50', 'text-blue-700');
      }
    });
    panels.forEach(p => {
      p.classList.toggle('hidden', p.id !== 'vrTab_' + tabName);
    });
    localStorage.setItem('vr_active_tab', tabName);
  }

  tabs.forEach(t => {
    t.addEventListener('click', () => {
      activate(t.dataset.tab);
    });
  });

  // default tab = date
  activate(localStorage.getItem('vr_active_tab') || 'date');

  // wiring filters
  const modeSel = document.getElementById('vrDateMode');
  const singleWrap = document.getElementById('vrDateSingleWrap');
  const rangeWrap = document.getElementById('vrDateRangeWrap');
  const dateSingle = document.getElementById('vrDateSingle');
  const dateFrom = document.getElementById('vrDateFrom');
  const dateTo = document.getElementById('vrDateTo');

  if (modeSel) {
    modeSel.addEventListener('change', () => {
      if (modeSel.value === 'range') {
        singleWrap.classList.add('hidden');
        rangeWrap.classList.remove('hidden');
      } else {
        singleWrap.classList.remove('hidden');
        rangeWrap.classList.add('hidden');
      }
    });
  }

  // fetchers
  async function fetchDateTab() {
    const today = new Date().toISOString().slice(0,10);
    const tbody = document.getElementById('vrDateTbody');
    const msg = document.getElementById('vrDateMsg');
    if (!tbody) return;

    tbody.innerHTML = '';
    msg.textContent = '';

    let url = '/api/view/date?mode=' + encodeURIComponent(modeSel.value || 'single');
    if (modeSel.value === 'range') {
      if (!dateFrom.value || !dateTo.value) {
        msg.textContent = 'Please select From and To.';
        return;
      }
      if (dateTo.value > today) {
        alert('Future To date selected. No data.');
        return;
      }
      url += `&from=${encodeURIComponent(dateFrom.value)}&to=${encodeURIComponent(dateTo.value)}`;
    } else {
      if (!dateSingle.value) {
        msg.textContent = 'Please select a date.';
        return;
      }
      if (dateSingle.value > today) {
        alert('Future date selected. No data.');
        return;
      }
      url += `&date=${encodeURIComponent(dateSingle.value)}`;
    }

    try {
      const res = await fetch(url);
      const data = await res.json();
      (data.rows || []).forEach(r => {
        const tr = document.createElement('tr');
        tr.className = 'border-b border-gray-200';
        tr.innerHTML = `
          <td class="p-3">${r.sr}</td>
          <td class="p-3">${r.first_name || ''}</td>
          <td class="p-3">${r.last_name || ''}</td>
          <td class="p-3">
            <a class="text-blue-600 hover:underline cursor-pointer" onclick="MGR.viewReport(${r.id})">View</a>
          </td>
        `;
        tbody.appendChild(tr);
      });
      if (data.message) msg.textContent = data.message;
      if ((data.rows || []).length === 0 && !data.message) {
        msg.textContent = 'No reports found.';
      }
    } catch (e) {
      alert('Failed to load data (Date tab).');
      console.error(e);
    }
  }

  async function fetchEmployeeTab() {
    const sel = document.getElementById('vrEmpSelect');
    const tbody = document.getElementById('vrEmpTbody');
    const msg = document.getElementById('vrEmpMsg');
    if (!sel || !tbody) return;

    tbody.innerHTML = '';
    msg.textContent = '';

    if (!sel.value) {
      msg.textContent = 'Select an employee.';
      return;
    }

    try {
      const res = await fetch(`/api/view/employee?employee=${encodeURIComponent(sel.value)}`);
      const data = await res.json();
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
      if ((data.rows || []).length === 0) {
        msg.textContent = 'No reports found for this employee.';
      }
    } catch (e) {
      alert('Failed to load data (Employee tab).');
      console.error(e);
    }
  }

  async function fetchSitesTab() {
    const sel = document.getElementById('vrSiteSelect');
    const date = document.getElementById('vrSiteDate');
    const tbody = document.getElementById('vrSiteTbody');
    const total = document.getElementById('vrSiteTotal');
    if (!sel || !tbody) return;

    tbody.innerHTML = '';
    total.textContent = '0.000';

    if (!sel.value) {
      return;
    }

    let url = `/api/view/sites?site=${encodeURIComponent(sel.value)}`;
    if (date && date.value) url += `&date=${encodeURIComponent(date.value)}`;

    try {
      const res = await fetch(url);
      const data = await res.json();
      (data.rows || []).forEach(r => {
        const tr = document.createElement('tr');
        tr.className = 'border-b border-gray-200';
        tr.innerHTML = `
          <td class="p-3">${r.sr}</td>
          <td class="p-3">${r.first_name || ''}</td>
          <td class="p-3">${r.last_name || ''}</td>
          <td class="p-3">${r.date}</td>
          <td class="p-3">
            <a class="text-blue-600 hover:underline cursor-pointer" onclick="MGR.viewReport(${r.id})">View</a>
          </td>
        `;
        tbody.appendChild(tr);
      });
      if (data.total_area != null) total.textContent = data.total_area;
    } catch (e) {
      alert('Failed to load data (Sites tab).');
      console.error(e);
    }
  }

  async function fetchDronesTab() {
    const sel = document.getElementById('vrDroneSelect');
    const date = document.getElementById('vrDroneDate');
    const tbody = document.getElementById('vrDroneTbody');
    const total = document.getElementById('vrDroneTotal');
    if (!sel || !tbody) return;

    tbody.innerHTML = '';
    total.textContent = '0';

    if (!sel.value) {
      return;
    }

    let url = `/api/view/drones?drone=${encodeURIComponent(sel.value)}`;
    if (date && date.value) url += `&date=${encodeURIComponent(date.value)}`;

    try {
      const res = await fetch(url);
      const data = await res.json();
      (data.rows || []).forEach(r => {
        const tr = document.createElement('tr');
        tr.className = 'border-b border-gray-200';
        tr.innerHTML = `
          <td class="p-3">${r.sr}</td>
          <td class="p-3">${r.first_name || ''}</td>
          <td class="p-3">${r.last_name || ''}</td>
          <td class="p-3">${r.date}</td>
          <td class="p-3">
            <a class="text-blue-600 hover:underline cursor-pointer" onclick="MGR.viewReport(${r.id})">View</a>
          </td>
        `;
        tbody.appendChild(tr);
      });
      if (data.total_flights != null) total.textContent = data.total_flights;
    } catch (e) {
      alert('Failed to load data (Drones tab).');
      console.error(e);
    }
  }

  // wire controls to fetch
  // Date tab changes
  if (modeSel) modeSel.addEventListener('change', fetchDateTab);
  if (dateSingle) dateSingle.addEventListener('change', fetchDateTab);
  if (dateFrom) dateFrom.addEventListener('change', fetchDateTab);
  if (dateTo) dateTo.addEventListener('change', fetchDateTab);

  const empSel = document.getElementById('vrEmpSelect');
  if (empSel) empSel.addEventListener('change', fetchEmployeeTab);

  const siteSel = document.getElementById('vrSiteSelect');
  const siteDate = document.getElementById('vrSiteDate');
  if (siteSel) siteSel.addEventListener('change', fetchSitesTab);
  if (siteDate) siteDate.addEventListener('change', fetchSitesTab);

  const droneSel = document.getElementById('vrDroneSelect');
  const droneDate = document.getElementById('vrDroneDate');
  if (droneSel) droneSel.addEventListener('change', fetchDronesTab);
  if (droneDate) droneDate.addEventListener('change', fetchDronesTab);

  // Refresh based on current tab, without resetting filters
  if (refresh) {
    refresh.addEventListener('click', () => {
      const active = localStorage.getItem('vr_active_tab') || 'date';
      if (active === 'date') return fetchDateTab();
      if (active === 'employee') return fetchEmployeeTab();
      if (active === 'sites') return fetchSitesTab();
      if (active === 'drones') return fetchDronesTab();
    });
  }

  // load default (date tab)
  fetchDateTab();
};

// keep existing MGR.viewReport, etc.
