import type { components } from './api-schema'

export type SupportRequest = components['schemas']['SupportRequest']

/**
 * Where a support request stands against the commitment (#53), in the words the
 * organisation reads: answered in time or late, or open with its deadline.
 */
export function supportStatus(request: SupportRequest): string {
  const due = new Date(request.respond_by).toUTCString().replace(':00 GMT', ' UTC')
  if (request.first_response_at) {
    return request.overdue ? `Answered late (due ${due})` : 'Answered in time'
  }
  return request.overdue ? `Late: a first response was due ${due}` : `First response due ${due}`
}
