import apiClient from './client';
import { API_ENDPOINTS } from '../utils/constants';

/**
 * Login with username and password
 */
export async function login(username, password) {
  const response = await apiClient.post(API_ENDPOINTS.LOGIN, {
    username,
    password,
  });
  return response.data;
}

/**
 * Refresh access token
 */
export async function refreshToken(refreshToken) {
  const response = await apiClient.post(API_ENDPOINTS.REFRESH, {
    refresh: refreshToken,
  });
  return response.data;
}
