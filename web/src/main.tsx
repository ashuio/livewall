import { createRoot } from 'react-dom/client';

import './tokens.css';
import './livewall/components.css';
import './App.css';
import App from './App';

// Deliberately NOT wrapped in <StrictMode>: StrictMode's double-invoked effects
// would tear down and rebuild the transport on every dev mount for no benefit
// here.
createRoot(document.getElementById('root')!).render(<App />);
