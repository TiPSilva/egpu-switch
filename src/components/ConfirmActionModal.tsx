import { ConfirmModal, Spinner } from '@decky/ui';
import { FC, useState } from 'react';

interface ConfirmActionModalProps {
  title: string;
  description: string;
  confirmText: string;
  onConfirm: () => Promise<void>;
  closeModal?(): void;
}

const ConfirmActionModal: FC<ConfirmActionModalProps> = ({
  title,
  description,
  confirmText,
  onConfirm,
  closeModal,
}) => {
  const [running, setRunning] = useState(false);
  return (
    <ConfirmModal
      closeModal={closeModal}
      bOKDisabled={running}
      bCancelDisabled={running}
      strTitle={
        <div style={{ display: 'flex', flexDirection: 'row', alignItems: 'center', width: '100%' }}>
          {title}
          {running && <Spinner width="20px" height="20px" style={{ marginLeft: 'auto' }} />}
        </div>
      }
      strOKButtonText={confirmText}
      onOK={async () => {
        setRunning(true);
        await onConfirm();
        closeModal?.();
      }}
    >
      {description}
    </ConfirmModal>
  );
};

export default ConfirmActionModal;
