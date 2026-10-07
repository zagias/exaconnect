const { api } = require('../lib');

module.exports = {
  key: 'send_message',
  noun: 'Message',
  display: { label: 'Send Message', description: "Sends a reply in a conversation, under the business's sending rules." },
  operation: {
    inputFields: [
      { key: 'conversation_id', label: 'Conversation', required: true },
      { key: 'body', label: 'Message', type: 'text' },
      { key: 'template', label: 'Template', helpText: 'Needed on WhatsApp outside the 24-hour window.' },
    ],
    perform: async (z, bundle) => {
      const { conversation_id: id, ...rest } = bundle.inputData;
      return api(z, bundle, 'POST', `/conversations/${encodeURIComponent(id)}/messages`, rest);
    },
  },
};
