// Proposes a connector action. Sensitive ones (refunds, payment links,
// messages to many people) wait for a person to approve them in CommAI.
const { api } = require('../lib');

module.exports = {
  key: 'propose_action',
  noun: 'Action',
  display: {
    label: 'Propose Action',
    description: 'Asks CommAI to run an action in a connected app, with approval where the business requires it.',
  },
  operation: {
    inputFields: [
      { key: 'app', label: 'App', required: true, helpText: 'Such as zendesk or shopify.' },
      { key: 'action', label: 'Action', required: true, helpText: 'Such as create_ticket.' },
      { key: 'inputs', label: 'Inputs', dict: true },
      { key: 'conversation_id', label: 'Conversation' },
    ],
    perform: async (z, bundle) => api(z, bundle, 'POST', '/actions', bundle.inputData),
  },
};
