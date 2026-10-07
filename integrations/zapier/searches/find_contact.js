const { api } = require('../lib');

module.exports = {
  key: 'find_contact',
  noun: 'Contact',
  display: { label: 'Find Contact', description: 'Finds a contact by name, email or phone.' },
  operation: {
    inputFields: [{ key: 'q', label: 'Search', required: true }],
    perform: async (z, bundle) =>
      (await api(z, bundle, 'GET', '/contacts', undefined, { q: bundle.inputData.q, limit: 5 })).items || [],
  },
};
