const { api } = require('../lib');

module.exports = {
  key: 'start_conversation',
  noun: 'Conversation',
  display: {
    label: 'Start Conversation',
    description: 'Starts a conversation with a customer, or adds to the one already open.',
  },
  operation: {
    inputFields: [
      { key: 'channel', label: 'Channel', choices: ['email', 'sms', 'whatsapp', 'webchat'], default: 'email' },
      { key: 'address', label: 'Address', required: true, helpText: 'Email address or phone number.' },
      { key: 'name', label: 'Name' },
      { key: 'subject', label: 'Subject' },
      { key: 'body', label: 'Message', type: 'text' },
      { key: 'external_id', label: 'Your reference', helpText: 'The same reference never starts two conversations.' },
    ],
    perform: async (z, bundle) => api(z, bundle, 'POST', '/conversations', bundle.inputData),
  },
};
