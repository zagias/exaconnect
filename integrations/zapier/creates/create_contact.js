const { api } = require('../lib');

module.exports = {
  key: 'create_contact',
  noun: 'Contact',
  display: { label: 'Create Contact', description: 'Adds a contact to CommAI.' },
  operation: {
    inputFields: [
      { key: 'name', label: 'Name' },
      { key: 'email', label: 'Email' },
      { key: 'phone', label: 'Phone', helpText: 'In international format, such as +12465550100.' },
      { key: 'external_ref', label: 'Your reference' },
    ],
    perform: async (z, bundle) => api(z, bundle, 'POST', '/contacts', bundle.inputData),
  },
};
