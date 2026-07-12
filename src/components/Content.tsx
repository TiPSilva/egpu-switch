import { ButtonItem, Field, PanelSection, PanelSectionRow, Spinner, ToggleField, showModal } from '@decky/ui';
import { FC, useCallback, useEffect, useRef, useState } from 'react';

import {
  ConnectionInfo,
  EgpuStatus,
  OpResult,
  disableEgpu,
  ejectEgpu,
  enableEgpu,
  getConnectionInfo,
  getSettings,
  getStatus,
  rescanPci,
  restartDisplayManager,
  setAutoEject,
  setDeepRescan,
} from '../backend';
import ConfirmActionModal from './ConfirmActionModal';

const POLL_INTERVAL_MS = 5000;

// Longest legitimate operation is Switch to eGPU with everything slow:
// pre-rescan status read (15s) + set-boot-vga retries (45s) + display
// manager restart (60s) ≈ 120s. Anything past 150s means the backend RPC
// wedged (seen on hardware: a sysfs write blocking in kernel); without
// this, `busy` never clears and every button stays disabled forever.
const OP_TIMEOUT_MS = 150000;

const withTimeout = (p: Promise<OpResult>): Promise<OpResult> =>
  Promise.race([
    p,
    new Promise<OpResult>((resolve) =>
      window.setTimeout(
        () =>
          resolve({
            ok: false,
            error:
              'Operation timed out after 150s. The backend may still be busy or stuck; ' +
              'check journalctl -u plugin_loader over SSH, or restart the plugin loader.',
          }),
        OP_TIMEOUT_MS,
      ),
    ),
  ]);

const Content: FC = () => {
  const [status, setStatus] = useState<EgpuStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  const [lastSuccess, setLastSuccess] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  const [connectionOpen, setConnectionOpen] = useState(false);
  const [connectionInfo, setConnectionInfo] = useState<ConnectionInfo | null>(null);
  const [connectionLoading, setConnectionLoading] = useState(false);

  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [autoEject, setAutoEjectState] = useState(false);
  const [deepRescan, setDeepRescanState] = useState(false);
  const [settingsLoaded, setSettingsLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await getStatus());
    } catch (e) {
      setLastError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    timerRef.current = window.setInterval(refresh, POLL_INTERVAL_MS);
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current);
    };
  }, [refresh]);

  useEffect(() => {
    getSettings()
      .then((s) => {
        setAutoEjectState(s.auto_eject);
        setDeepRescanState(s.deep_rescan);
      })
      .catch((e) => setLastError(String(e)))
      .finally(() => setSettingsLoaded(true));
  }, []);

  const runGuarded = async (fn: () => Promise<OpResult>) => {
    if (busy) return;
    setBusy(true);
    setLastError(null);
    setLastSuccess(null);
    try {
      const res = await withTimeout(fn());
      if (!res.ok) setLastError(res.error ?? 'Unknown error');
      else if (res.message) setLastSuccess(res.message);
    } catch (e) {
      setLastError(String(e));
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const toggleDescription = (toEgpu: boolean): string => {
    if (toEgpu) {
      return "This restarts the display manager: the screen will flicker and your current session will briefly close and reopen (can take 5-15s). This is expected.";
    }
    if (autoEject) {
      return "This restarts the display manager and, if an eGPU is connected, safely ejects it in the same step: the screen will flicker and your current session will briefly close and reopen (can take 5-15s). This is expected.";
    }
    return "This restarts the display manager: the screen will flicker and your current session will briefly close and reopen (can take 5-15s). This is expected. The eGPU is not ejected automatically here; use Eject eGPU separately before disconnecting the cable.";
  };

  const confirmToggle = () => {
    if (!status) return;
    const toEgpu = !status.egpu_active;
    showModal(
      <ConfirmActionModal
        title={toEgpu ? 'Switch to eGPU?' : 'Switch to iGPU?'}
        description={toggleDescription(toEgpu)}
        confirmText={toEgpu ? 'Switch to eGPU' : 'Switch to iGPU'}
        onConfirm={() => runGuarded(toEgpu ? enableEgpu : disableEgpu)}
      />,
    );
  };

  const confirmRestart = () => {
    showModal(
      <ConfirmActionModal
        title="Restart Display Manager?"
        description="Recovery action only, does not change GPU configuration. Restarts the display manager to unstick a bad display state; your session will briefly close and reopen."
        confirmText="Restart Display Manager"
        onConfirm={() => runGuarded(restartDisplayManager)}
      />,
    );
  };

  const confirmEject = () => {
    showModal(
      <ConfirmActionModal
        title="Eject eGPU?"
        description="Stops the display manager, unloads the nvidia kernel modules and detaches the eGPU from the PCI bus, then restarts the session: the screen will flicker again, same as switching GPU. The nvidia driver does not support surprise Thunderbolt disconnects, so do NOT unplug the cable until you see the 'safe to disconnect' confirmation."
        confirmText="Eject eGPU"
        onConfirm={() => runGuarded(ejectEgpu)}
      />,
    );
  };

  const statusLine = (() => {
    if (!status) return 'Loading…';
    if (!status.installed) return 'all-ways-egpu is not installed on this system.';
    if (!status.setup_done) return "Not configured. Run 'all-ways-egpu setup' once from a terminal.";
    if (!status.egpu_connected) return 'eGPU configured but not detected (check Thunderbolt cable).';
    return status.egpu_active
      ? `eGPU active (${status.gpu_name ?? status.bus_id})`
      : `eGPU connected, iGPU active (${status.gpu_name ?? status.bus_id})`;
  })();

  const toggleDisabled = busy || !status?.installed || !status?.setup_done;
  const ejectDisabled = busy || !status?.egpu_connected || !!status?.egpu_active;

  // Safe to unplug whenever nothing is currently bound to the eGPU's PCI
  // slot: covers both "was never connected" and "successfully ejected while
  // the cable is still plugged in" (the device disappears from lspci either
  // way). The two "not safe" reasons are distinguished with a sub-line so
  // "idle but connected" doesn't read as an unexplained false alarm (this is
  // exactly the case that confused an early tester).
  const safety = (() => {
    if (!status?.installed || !status?.setup_done) {
      return { color: '#94a3b8', label: 'N/A', sub: null as string | null };
    }
    if (!status.egpu_connected) {
      return { color: '#4ade80', label: 'Safe', sub: null };
    }
    if (status.egpu_active) {
      return {
        color: '#f87171',
        label: 'Not safe (in use)',
        sub: 'eGPU is actively driving the display.',
      };
    }
    return {
      color: '#f87171',
      label: 'Not safe (not ejected)',
      sub: 'eGPU is idle but not yet ejected. Press Eject eGPU before disconnecting.',
    };
  })();

  const toggleConnection = () => {
    const next = !connectionOpen;
    setConnectionOpen(next);
    if (next && !connectionInfo && !connectionLoading) {
      setConnectionLoading(true);
      getConnectionInfo()
        .then(setConnectionInfo)
        .catch((e) => setLastError(String(e)))
        .finally(() => setConnectionLoading(false));
    }
  };

  const handleAutoEjectChange = async (checked: boolean) => {
    setAutoEjectState(checked);
    const res = await setAutoEject(checked);
    if (!res.ok) {
      setLastError(res.error ?? 'Failed to save setting');
      setAutoEjectState(!checked);
    }
  };

  const handleDeepRescanChange = async (checked: boolean) => {
    setDeepRescanState(checked);
    const res = await setDeepRescan(checked);
    if (!res.ok) {
      setLastError(res.error ?? 'Failed to save setting');
      setDeepRescanState(!checked);
    }
  };

  return (
    <PanelSection title="eGPU Switch">
      <PanelSectionRow>
        <Field label="Thunderbolt cable" focusable>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span
                style={{
                  display: 'inline-block',
                  width: '10px',
                  height: '10px',
                  borderRadius: '50%',
                  backgroundColor: safety.color,
                  flexShrink: 0,
                }}
              />
              <span>{safety.label}</span>
            </div>
            {safety.sub && <span style={{ fontSize: '12px', opacity: 0.7 }}>{safety.sub}</span>}
          </div>
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <Field label="Status">{statusLine}</Field>
      </PanelSectionRow>
      {lastError && (
        <PanelSectionRow>
          <Field label="Last error" focusable>
            {lastError}
          </Field>
        </PanelSectionRow>
      )}
      {lastSuccess && (
        <PanelSectionRow>
          <Field label="Done" focusable>
            <span style={{ color: '#4ade80' }}>{lastSuccess}</span>
          </Field>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={toggleDisabled} onClick={confirmToggle}>
          {busy ? <Spinner width="16px" height="16px" /> : status?.egpu_active ? 'Switch to iGPU' : 'Switch to eGPU'}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={ejectDisabled} onClick={confirmEject}>
          Eject eGPU
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={confirmRestart}>
          Restart Display Manager (recovery)
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={() => runGuarded(rescanPci)}>
          Rescan for eGPU
        </ButtonItem>
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={toggleConnection}>
          {connectionOpen ? '▾ Connection' : '▸ Connection'}
        </ButtonItem>
      </PanelSectionRow>
      {connectionOpen && connectionLoading && (
        <PanelSectionRow>
          <Field label="Connection" focusable>
            <Spinner width="16px" height="16px" />
          </Field>
        </PanelSectionRow>
      )}
      {connectionOpen && !connectionLoading && connectionInfo && (
        <>
          <PanelSectionRow>
            <Field label="PCIe link" focusable>
              {connectionInfo.pcie_generation && connectionInfo.pcie_width
                ? `${connectionInfo.pcie_generation} ${connectionInfo.pcie_width} (${connectionInfo.pcie_speed})`
                : 'Not available'}
            </Field>
          </PanelSectionRow>
          {connectionInfo.thunderbolt_generation && (
            <PanelSectionRow>
              <Field label="Thunderbolt" focusable>
                {connectionInfo.thunderbolt_generation}
                {connectionInfo.thunderbolt_rx_speed ? `, ${connectionInfo.thunderbolt_rx_speed}` : ''}
              </Field>
            </PanelSectionRow>
          )}
          {connectionInfo.thunderbolt_name && (
            <PanelSectionRow>
              <Field label="Controller" focusable>
                {connectionInfo.thunderbolt_name}
              </Field>
            </PanelSectionRow>
          )}
        </>
      )}
      {connectionOpen && !connectionLoading && !connectionInfo && (
        <PanelSectionRow>
          <Field label="Connection" focusable>
            Not available
          </Field>
        </PanelSectionRow>
      )}

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => setAdvancedOpen(!advancedOpen)}>
          {advancedOpen ? '▾ Advanced' : '▸ Advanced'}
        </ButtonItem>
      </PanelSectionRow>
      {advancedOpen && (
        <PanelSectionRow>
          <ToggleField
            label="Automatic eject (experimental)"
            description="Ejects the eGPU automatically as part of Switch to iGPU, so the cable is safe to disconnect right away. Off by default: you press Eject eGPU separately. See README for a rare edge case this can hit."
            checked={autoEject}
            disabled={!settingsLoaded}
            onChange={handleAutoEjectChange}
          />
        </PanelSectionRow>
      )}
      {advancedOpen && (
        <PanelSectionRow>
          <ToggleField
            label="Deep rescan (experimental)"
            description="Experimental. When the eGPU is missing from the PCI bus, also removes its parent PCI bridge before rescanning (briefly affects anything else on that port); does nothing extra while the eGPU is present. Only enable if the eGPU stays undetected after Eject/disconnect even with a plain rescan. See README for details."
            checked={deepRescan}
            disabled={!settingsLoaded}
            onChange={handleDeepRescanChange}
          />
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};

export default Content;
