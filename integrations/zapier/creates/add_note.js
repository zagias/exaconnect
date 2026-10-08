const { api } = require('../lib');

module.exports = {
  key: 'add_note',
  noun: 'Note',
  display: { label: 'Add Internal Note', description: 'Adds a note staff can see; the customer never does.' },
  operation: {
    inputFields: [
      { key: 'conversation_id', label: 'Conversation', required: true },
      { key: 'body', label: 'Note', type: 'text', required: true },
    ],
    perform: async (z, bundle) =>
      api(z, bundle, 'POST', `/conversations/${encodeURIComponent(bundle.inputData.conversation_id)}/notes`, {
        body: bundle.inputData.body,
      }),
  },
};
