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

export const getStatus = () => call<[], EgpuStatus>('get_status');
export const enableEgpu = () => call<[], OpResult>('enable_egpu');
export const disableEgpu = () => call<[], OpResult>('disable_egpu');
export const restartDisplayManager = () => call<[], OpResult>('restart_display_manager');
export const ejectEgpu = () => call<[], OpResult>('eject_egpu');
export const rescanPci = () => call<[], OpResult>('rescan_pci');
