import { ButtonItem, Field, PanelSection, PanelSectionRow, Spinner, showModal } from '@decky/ui';
import { FC, useCallback, useEffect, useRef, useState } from 'react';

import {
  EgpuStatus,
  OpResult,
  disableEgpu,
  ejectEgpu,
  enableEgpu,
  getStatus,
  rescanPci,
  restartDisplayManager,
} from '../backend';
import ConfirmActionModal from './ConfirmActionModal';

const POLL_INTERVAL_MS = 5000;

const Content: FC = () => {
  const [status, setStatus] = useState<EgpuStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  const [lastSuccess, setLastSuccess] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

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

  const runGuarded = async (fn: () => Promise<OpResult>) => {
    if (busy) return;
    setBusy(true);
    setLastError(null);
    setLastSuccess(null);
    try {
      const res = await fn();
      if (!res.ok) setLastError(res.error ?? 'Unknown error');
      else if (res.message) setLastSuccess(res.message);
    } catch (e) {
      setLastError(String(e));
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const confirmToggle = () => {
    if (!status) return;
    const toEgpu = !status.egpu_active;
    showModal(
      <ConfirmActionModal
        title={toEgpu ? 'Switch to eGPU?' : 'Switch to iGPU?'}
        description={
          toEgpu
            ? "This restarts the display manager: the screen will flicker and your current session will briefly close and reopen (can take 5-15s). This is expected."
            : "This restarts the display manager and, if an eGPU is connected, safely ejects it in the same step: the screen will flicker and your current session will briefly close and reopen (can take 5-15s). This is expected."
        }
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
  // way, so no extra state needs to be tracked here).
  const safety = (() => {
    if (!status?.installed || !status?.setup_done) return { color: '#94a3b8', label: 'N/A' };
    return status.egpu_connected
      ? { color: '#f87171', label: 'Not safe' }
      : { color: '#4ade80', label: 'Safe' };
  })();

  return (
    <PanelSection title="eGPU Switch">
      <PanelSectionRow>
        <Field label="Thunderbolt cable" focusable>
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
    </PanelSection>
  );
};

export default Content;
