import { definePlugin } from '@decky/api';
import { FaBolt } from 'react-icons/fa';

import Content from './components/Content';

export default definePlugin(() => ({
  name: 'eGPU Switch',
  titleView: <div>eGPU Switch</div>,
  content: <Content />,
  icon: <FaBolt />,
}));
