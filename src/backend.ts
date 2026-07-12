import { call } from '@decky/api';

export interface EgpuStatus {
  installed: boolean;
  setup_done: boolean;
  egpu_connected: boolean;
  egpu_active: boolean;
  bus_id: string | null;
  driver: string | null;
  gpu_name: string | null;
  error: string | null;
}

export interface OpResult {
  ok: boolean;
  error?: string;
  message?: string;
  cli_output?: string;
  restart?: { ok: boolean; error?: string };
  removed?: string[];
}

export interface ConnectionInfo {
  pcie_generation: string | null;
  pcie_speed: string | null;
  pcie_width: string | null;
  thunderbolt_generation: string | null;
  thunderbolt_rx_speed: string | null;
  thunderbolt_tx_speed: string | null;
  thunderbolt_name: string | null;
}

export interface Settings {
  auto_eject: boolean;
  deep_rescan: boolean;
}

export const getStatus = () => call<[], EgpuStatus>('get_status');
export const enableEgpu = () => call<[], OpResult>('enable_egpu');
export const disableEgpu = () => call<[], OpResult>('disable_egpu');
export const restartDisplayManager = () => call<[], OpResult>('restart_display_manager');
export const ejectEgpu = () => call<[], OpResult>('eject_egpu');
export const rescanPci = () => call<[], OpResult>('rescan_pci');
export const getConnectionInfo = () => call<[], ConnectionInfo>('get_connection_info');
export const getSettings = () => call<[], Settings>('get_settings');
export const setAutoEject = (enabled: boolean) => call<[boolean], OpResult>('set_auto_eject', enabled);
export const setDeepRescan = (enabled: boolean) => call<[boolean], OpResult>('set_deep_rescan', enabled);
