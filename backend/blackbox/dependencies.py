"""Cheap startup inventory; never installs packages or invokes a model."""
import shutil

DEPENDENCIES = {
    'iputils': ('baseline', ('ping',), 'ICMP reachability unavailable'),
    'nmap': ('baseline', ('nmap',), 'Port scanning unavailable; existing results can still be analyzed'),
    'openssh': ('baseline', ('ssh',), 'Native OpenSSH unavailable; library-backed SFTP still requires a configured profile'),
    'ffuf': ('preferred', ('ffuf',), 'HTTP discovery uses bounded native HTTP fallback'),
    'whatweb': ('optional', ('whatweb',), 'Web fingerprinting unavailable; HTTP headers remain available'),
    'grim': ('optional', ('grim',), 'Native screenshot capture unavailable; browser uploads still work'),
    'wl-clipboard': ('optional', ('wl-copy', 'wl-paste'), 'Native clipboard utilities unavailable; browser paste/uploads still work'),
}


def detect():
    binaries = {name: shutil.which(name) for _, names, _ in DEPENDENCIES.values() for name in names}
    packages = {package: {'tier':tier, 'available':all(binaries[n] for n in names),
                          'binaries':list(names), 'impact':impact,
                          'install':f'sudo pacman -S --needed {package}'}
                for package, (tier, names, impact) in DEPENDENCIES.items()}
    return {'packages':packages, 'binaries':binaries,
            'baseline_ready':all(p['available'] for p in packages.values() if p['tier']=='baseline'),
            'capabilities':{'port_scan':bool(binaries['nmap']), 'ping':bool(binaries['ping']),
                            'http_discovery':'ffuf' if binaries['ffuf'] else 'native_http',
                            'web_fingerprinting':bool(binaries['whatweb']),
                            'screenshot_upload':True, 'browser_image_paste':True},
            'messages':[f"{p['tier'].capitalize()} dependency {name} missing: {p['impact']}. Install: {p['install']}"
                        for name,p in packages.items() if not p['available']]}
