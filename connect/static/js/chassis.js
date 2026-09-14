/**
 * Chassis diagram interactive logic.
 * This file supplements the inline <script> in chassis_detail.html
 * for any reusable or advanced features.
 */

(function() {
    'use strict';

    /**
     * Export CSV of all ports data from the table.
     */
    window.exportPortsCSV = function() {
        const table = document.getElementById('portsTable');
        if (!table) return;
        const rows = table.querySelectorAll('tr');
        let csv = [];
        rows.forEach(function(row) {
            const cols = row.querySelectorAll('th, td');
            let rowData = [];
            cols.forEach(function(col, idx) {
                if (idx === 0) return; // skip checkbox column
                let text = col.textContent.trim().replace(/"/g, '""');
                rowData.push('"' + text + '"');
            });
            csv.push(rowData.join(','));
        });
        const blob = new Blob([csv.join('\n')], { type: 'text/csv' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'ports_export.csv';
        a.click();
        URL.revokeObjectURL(url);
    };

    /**
     * Keyboard shortcut: Escape to deselect all ports
     */
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            const btn = document.getElementById('btnDeselectAll');
            if (btn) btn.click();
        }
    });

})();
