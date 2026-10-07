// REST hook: subscribing adds a Jibsy webhook endpoint for one event type;
// unsubscribing deletes it. Each delivery is checked against the signing
// secret Jibsy returned when the endpoint was made.
const { api, verify } = require('../lib');

const tidy = (e) => ({ id: e.id, type: e.type, created_at: e.created_at || e.at, data: e.data || {} });

module.exports = {
  key: 'new_event',
  noun: 'Event',
  display: {
    label: 'New Event',
    description: 'Triggers when something happens in Jibsy, such as a new conversation or a handover.',
  },
  operation: {
    type: 'hook',
    inputFields: [{ key: 'event_type', label: 'Event', required: true, dynamic: 'event_types.id.id' }],
    performSubscribe: async (z, bundle) =>
      api(z, bundle, 'POST', '/webhooks', {
        url: bundle.targetUrl,
        description: 'Zapier',
        events: [bundle.inputData.event_type],
      }),
    performUnsubscribe: async (z, bundle) => api(z, bundle, 'DELETE', `/webhooks/${bundle.subscribeData.id}`),
    perform: async (z, bundle) => {
      const raw = bundle.rawRequest || {};
      if (!verify(bundle.subscribeData.secret, raw.headers, raw.content)) {
        throw new z.errors.HaltedError('The delivery was not signed by Jibsy.');
      }
      return [tidy(bundle.cleanedRequest)];
    },
    performList: async (z, bundle) => {
      const page = await api(z, bundle, 'GET', '/events', undefined, { type: bundle.inputData.event_type, limit: 3 });
      return (page.items || []).reverse().map(tidy);
    },
    sample: {
      id: 'evt_example',
      type: 'conversation.created',
      created_at: '2026-10-01T12:00:00Z',
      data: { conversation_id: 'example' },
    },
  },
};
