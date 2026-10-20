import { useJobs } from './useJobs';
import { useNotification } from './useNotification';
import { useApiErrorMessage } from './useApiErrorMessage';
import type { ServerEvent } from './useEvents';

export const notifyRequeueOutcome = (
  result: Extract<ServerEvent, { type: 'job.requeue' }>['payload'],
): void => {
  const notify = useNotification();
  const reason = result.reason || result.decision?.reason;
  const detail = reason ? `: ${reason}` : '.';
  switch (result.outcome) {
    case 'queued':
      return;
    case 'not_queued':
      notify.info(`Job #${result.job_id} not queued${detail}`);
      return;
    case 'failed':
      notify.error(`Requeue failed for job #${result.job_id}${detail}`);
      return;
    case 'cancelled':
      notify.info(`Requeue cancelled for job #${result.job_id}${detail}`);
  }
};

export const useJobRequeue = () => {
  const { runAction, runBulk } = useJobs();
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();

  const requeue = async (id: number): Promise<boolean> => {
    const result = await runAction(id, 'retry');
    if (!result.ok) {
      notify.error(messageFor(result.error.detail, 'Request failed.'));
      return false;
    }
    notify.success(`Requeue requested for job #${id}.`);
    return true;
  };

  const requeueBulk = async (ids: number[]): Promise<number[] | null> => {
    const result = await runBulk('retry', ids);
    if ('error' in result) {
      notify.error(messageFor(result.error.detail, 'Request failed.'));
      return null;
    }
    const count = result.accepted.length;
    const summary = `Requeue requested for ${count} job${count === 1 ? '' : 's'}. ${result.failed.length} rejected.`;
    if (result.failed.length) {
      const first = result.failed[0]!;
      notify.error(`${summary} First rejection: #${first.id}: ${first.detail}`);
    } else if (count) notify.success(summary);
    else notify.info(summary);
    return result.accepted;
  };

  return { requeue, requeueBulk };
};
