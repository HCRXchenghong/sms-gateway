/* Recovery-code modal for the account-security tab. */
(function() {
    'use strict';
    function $(s, r) { return (r || document).querySelector(s); }
    function codes() {
        return Array.from(document.querySelectorAll('#recovery-list li')).map(function(n) { return n.textContent; });
    }
    function close() { $('#recovery-modal').hidden = true; }
    document.addEventListener('DOMContentLoaded', function() {
        var modal = $('#recovery-modal');
        if (!modal) return;
        $('#close-recovery-modal').addEventListener('click', close);
        $('#done-recovery').addEventListener('click', close);
        modal.addEventListener('click', function(e) { if (e.target === modal) close(); });
        $('#copy-recovery').addEventListener('click', function() {
            navigator.clipboard.writeText(codes().join('\n')).then(function() {
                $('#copy-recovery').textContent = '已复制';
                setTimeout(function() { $('#copy-recovery').textContent = '复制'; }, 1800);
            });
        });
        $('#download-recovery').addEventListener('click', function() {
            var blob = new Blob([codes().join('\n') + '\n'], { type: 'text/plain;charset=utf-8' });
            var a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'recovery-codes.txt';
            a.click();
            URL.revokeObjectURL(a.href);
        });
    });
})();
