// Hidden trigger that fills the Event drop-down.
const { api } = require('../lib');

module.exports = {
  key: 'event_types',
  noun: 'Event type',
  display: { label: 'Event types', description: 'Lists CommAI event types.', hidden: true },
  operation: {
    perform: async (z, bundle) => (await api(z, bundle, 'GET', '/event-types')).map((t) => ({ id: t })),
  },
};
